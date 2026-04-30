"""LaunchDarkly mobile/client-side load emulator.

Strategy: replace only the *surface* the Python SDK doesn't model (the mobile
streaming/polling/events URLs, the ``api_key`` auth prefix, the absence of
index events) and reuse ``ldclient`` machinery for everything else - event
buffering, summary aggregation, debug-event windowing, anonymous-attribute
redaction, private-attribute filtering, sampling, gzip, retries, payload-id
headers, diagnostic init/stats, and so on.

The reference for mobile wire behavior is the iOS client SDK at
``reference/ios-client-sdk``. Notable points where mobile differs from the
Python *server* SDK:

- ``Authorization: api_key <mobileKey>`` (server uses the raw key)
- events POST to ``/mobile/events/bulk``; diagnostics POST to
  ``/mobile/events/diagnostic`` (server uses ``/bulk`` and ``/diagnostic``)
- streaming endpoint ``/meval/{base64(context)}``; polling fallback
  ``/msdk/evalx/contexts/{base64(context)}``
- mobile SDKs do **not** emit index events
- mobile flag payloads carry an evaluated ``value`` plus ``flagVersion``
  (used as ``versionForEvents``), ``trackEvents``, ``debugEventsUntilDate``,
  ``trackReason`` - they are not full flag definitions, so the standard
  evaluator cannot run on them
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from base64 import urlsafe_b64encode
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import urllib3
from ld_eventsource import SSEClient
from ld_eventsource.actions import Event, Fault, Start
from ld_eventsource.config import (
    ConnectStrategy,
    ErrorStrategy,
    RetryDelayStrategy,
)
from ldclient.config import Config, HTTPConfig
from ldclient.context import Context
from ldclient.impl.events.diagnostics import (
    _DiagnosticAccumulator,
    create_diagnostic_id,
)
from ldclient.impl.events.event_processor import EventDispatcher
from ldclient.impl.events.types import (
    EventFactory,
    EventInputEvaluation,
)

from locust import events as locust_events

from .server import LaunchDarklyLocust, LocustEventDispatcher
from .transport import clean_name, create_http_pool_manager, mobile_headers

log = logging.getLogger(__name__)

# Endpoints (match iOS DarklyService)
MEVAL_PATH = "/meval"
EVALX_CONTEXTS_PATH = "/msdk/evalx/contexts"

# Mobile defaults (match iOS LDConfig.Defaults)
DEFAULT_EVENT_FLUSH_INTERVAL = 30.0
DEFAULT_EVENT_CAPACITY = 100
DEFAULT_DIAGNOSTIC_INTERVAL = 900.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 10.0
STREAM_READ_TIMEOUT = 5 * 60

HEARTBEAT_FLAG_KEY = "locust-heartbeat"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _context_b64(context: Context) -> str:
    return urlsafe_b64encode(context.to_json_string().encode("utf-8")).decode("ascii")


@contextlib.contextmanager
def _eval_latency_metric(method: str) -> Iterator[None]:
    """Time a flag evaluation and emit an ``ld:eval`` Locust request event."""
    start = time.perf_counter()
    exc: Exception | None = None
    try:
        yield
    except Exception as e:
        exc = e
        raise
    finally:
        locust_events.request.fire(
            request_type="ld:eval",
            name=f"mobile.{method}",
            response_time=int((time.perf_counter() - start) * 1000),
            response_length=0,
            response=None,
            exception=exc,
            context={},
            url=method,
        )


# ---------------------------------------------------------------------------
# MobileConfig: only the bits where mobile differs from server
# ---------------------------------------------------------------------------


class MobileConfig(Config):
    """Config for the mobile emulator.

    Differences from a vanilla server ``Config``:

    - ``sdk_key`` returns ``api_key <key>`` so every request the SDK sends gets
      the mobile-style auth prefix. The bare key is still validated/stored
      under ``Config.__sdk_key``.
    - ``events_base_uri`` carries ``/mobile/events`` so the SDK's hardcoded
      ``+ /bulk`` and ``+ /diagnostic`` suffixes land on the right paths.
    - default ``flush_interval`` / ``events_max_pending`` /
      ``diagnostic_recording_interval`` match iOS.
    """

    def __init__(
        self,
        mobile_key: str,
        *,
        events_uri: str = "https://mobile.launchdarkly.com",
        flush_interval: float = DEFAULT_EVENT_FLUSH_INTERVAL,
        events_max_pending: int = DEFAULT_EVENT_CAPACITY,
        diagnostic_recording_interval: int = int(DEFAULT_DIAGNOSTIC_INTERVAL),
        **kwargs: Any,
    ) -> None:
        super().__init__(
            sdk_key=mobile_key,
            events_uri=events_uri,
            flush_interval=flush_interval,
            events_max_pending=events_max_pending,
            diagnostic_recording_interval=diagnostic_recording_interval,
            **kwargs,
        )
        self._mobile_key = mobile_key

    @property
    def sdk_key(self) -> str | None:  # type: ignore[override]
        # Mobile SDKs send Authorization: api_key <mobileKey>
        raw = self._mobile_key
        return f"api_key {raw}" if raw else ""

    @property
    def events_base_uri(self) -> str:  # type: ignore[override]
        return self._Config__events_uri + "/mobile/events"  # type: ignore[attr-defined]

    @property
    def events_uri(self) -> str:  # type: ignore[override]
        return self.events_base_uri + "/bulk"


# ---------------------------------------------------------------------------
# Flag store (mobile flag payloads, not full flag definitions)
# ---------------------------------------------------------------------------


class _FlagStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flags: dict[str, dict[str, Any]] = {}
        self._initialized = False

    def init(self, flags: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self._flags = {k: {**v, "key": k} for k, v in flags.items()}
            self._initialized = True

    def upsert(self, flag: dict[str, Any]) -> None:
        key = flag.get("key")
        if not key:
            return
        with self._lock:
            existing = self._flags.get(key)
            if existing and existing.get("version", -1) >= flag.get("version", 0):
                return
            self._flags[key] = flag

    def delete(self, key: str, version: int) -> None:
        with self._lock:
            existing = self._flags.get(key)
            if not existing or existing.get("version", -1) < version:
                self._flags[key] = {"key": key, "version": version, "deleted": True}

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            flag = self._flags.get(key)
            return None if flag is None or flag.get("deleted") else flag

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: v for k, v in self._flags.items() if not v.get("deleted")}

    @property
    def initialized(self) -> bool:
        return self._initialized


# ---------------------------------------------------------------------------
# FlagProxy: shaped like ldclient.impl.model.FeatureFlag for the bits the
# event subsystem reads. Built from a mobile flag dict.
# ---------------------------------------------------------------------------


class _FlagProxy:
    """Adapter from a mobile-flag-state dict to the ``FeatureFlag``-shaped
    surface the SDK's event processor expects.

    Read paths in the SDK:
    - ``flag.version`` (event payload, summarizer counter key)
    - ``flag.track_events`` (gates feature events)
    - ``flag.track_events_fallthrough`` (``EventFactory.is_experiment``)
    - ``flag.debug_events_until_date`` (gates debug events)
    - ``flag.sampling_ratio`` / ``flag.exclude_from_summaries`` (event sampling)
    - ``flag.rules[i].track_events`` (``EventFactory.is_experiment``)

    For mobile, ``versionForEvents = flagVersion ?? version`` - we surface that
    via ``version`` so the emitted event has the mobile-correct value.
    """

    __slots__ = (
        "key",
        "version",
        "track_events",
        "track_events_fallthrough",
        "debug_events_until_date",
        "sampling_ratio",
        "exclude_from_summaries",
        "rules",
    )

    def __init__(self, flag: dict[str, Any]) -> None:
        self.key = flag.get("key", "")
        # versionForEvents semantics
        self.version = flag.get("flagVersion") or flag.get("version") or 0
        self.track_events = bool(flag.get("trackEvents"))
        self.track_events_fallthrough = bool(flag.get("trackReason"))
        self.debug_events_until_date = flag.get("debugEventsUntilDate")
        self.sampling_ratio = flag.get("samplingRatio") or 1
        self.exclude_from_summaries = bool(flag.get("excludeFromSummaries"))
        self.rules: list = []  # mobile payloads do not carry rules


class _MobileEventDispatcher(EventDispatcher):
    """``EventDispatcher`` minus index events (mobile SDKs don't emit them)."""

    def _get_indexable_context(self, event, block) -> None:  # type: ignore[override]
        return None


class _MobileFeatureRequester:
    """Polling fallback used when a ``ping`` event is received."""

    def __init__(self, base_uri: str, mobile_key: str, verify_ssl: bool, http_proxy: str | None) -> None:
        self._base_uri = base_uri.rstrip("/")
        self._mobile_key = mobile_key
        self._http = create_http_pool_manager(
            num_pools=1,
            verify_ssl=verify_ssl,
            target_base_uri=self._base_uri,
            force_proxy=http_proxy,
        )

    def get_all(self, context: Context) -> dict[str, dict[str, Any]]:
        uri = f"{self._base_uri}{EVALX_CONTEXTS_PATH}/{_context_b64(context)}"
        hdrs = mobile_headers(self._mobile_key)
        hdrs["Accept"] = "application/json"
        resp = self._http.request(
            "GET",
            uri,
            headers=hdrs,
            timeout=urllib3.Timeout(connect=DEFAULT_CONNECT_TIMEOUT, read=DEFAULT_READ_TIMEOUT),
            retries=1,
        )
        if resp.status >= 400:
            raise RuntimeError(f"flag fetch returned {resp.status}")
        return json.loads(resp.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Public mobile client
# ---------------------------------------------------------------------------


class MobileLDClient:
    """Mobile/client-side LaunchDarkly load emulator.

    Public surface mirrors the official mobile SDKs as closely as feasible:
    ``variation``, ``variation_detail``, ``all_flags_state``, ``identify``,
    ``track``, ``flush``, ``close``, ``is_initialized``.
    """

    def __init__(
        self,
        context: Context,
        sdk_key: str,
        *,
        base_uri: str = "https://clientsdk.launchdarkly.com",
        stream_uri: str = "https://clientstream.launchdarkly.com",
        events_uri: str = "https://mobile.launchdarkly.com",
        send_events: bool = False,
        verify_ssl: bool = True,
        http_proxy: str | None = None,
        evaluation_reasons: bool = False,
        diagnostic_opt_out: bool = False,
        enable_event_compression: bool = False,
        event_flush_interval: float = DEFAULT_EVENT_FLUSH_INTERVAL,
        event_capacity: int = DEFAULT_EVENT_CAPACITY,
        diagnostic_recording_interval: float = DEFAULT_DIAGNOSTIC_INTERVAL,
        all_attributes_private: bool = False,
        private_attributes: set | None = None,
        omit_anonymous_contexts: bool = False,
        start_wait: float = 5.0,
        initial_reconnect_delay: float = 1.0,
    ) -> None:
        if context is None or not context.valid:
            raise ValueError("context is required for the mobile client")
        if not sdk_key:
            raise ValueError("sdk_key is required for the mobile client")

        self._context = context
        self._sdk_key = sdk_key  # raw key, no prefix
        self._base_uri = base_uri.rstrip("/")
        self._stream_uri = stream_uri.rstrip("/")
        self._evaluation_reasons = evaluation_reasons
        self._initial_reconnect_delay = initial_reconnect_delay
        self._verify_ssl = verify_ssl
        self._http_proxy = http_proxy

        self._config = MobileConfig(
            mobile_key=sdk_key,
            base_uri=base_uri,
            events_uri=events_uri,
            stream_uri=stream_uri,
            send_events=send_events,
            flush_interval=event_flush_interval,
            events_max_pending=event_capacity,
            diagnostic_opt_out=diagnostic_opt_out,
            diagnostic_recording_interval=int(diagnostic_recording_interval),
            enable_event_compression=enable_event_compression,
            all_attributes_private=all_attributes_private,
            private_attributes=private_attributes or set(),
            omit_anonymous_contexts=omit_anonymous_contexts,
            http=HTTPConfig(
                connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                read_timeout=DEFAULT_READ_TIMEOUT,
                http_proxy=http_proxy,
                disable_ssl_verification=not verify_ssl,
            ),
        )

        self._store = _FlagStore()
        self._requester = _MobileFeatureRequester(self._base_uri, sdk_key, verify_ssl, http_proxy)

        self._event_factory_default = EventFactory(False)
        self._event_factory_with_reasons = EventFactory(True)

        # Use the upstream DefaultEventProcessor (via LocustEventDispatcher's
        # instrumented HTTP wrapper) but parameterized with our index-event-
        # suppressing dispatcher subclass. That alone covers summary, debug,
        # sampling, gzip, retries, payload-id headers, and diagnostic init/
        # stats - we own only the mobile-specific deltas.
        self._diagnostic_accumulator: _DiagnosticAccumulator | None = None
        self._event_processor = None
        if send_events:
            if not diagnostic_opt_out:
                self._diagnostic_accumulator = _DiagnosticAccumulator(create_diagnostic_id(self._config))
            self._event_processor = LocustEventDispatcher(
                self._config,
                diagnostic_accumulator=self._diagnostic_accumulator,
                dispatcher_class=_MobileEventDispatcher,
            )

        self._closed_flag = threading.Event()
        self._ready = threading.Event()
        self._sse: SSEClient | None = None
        self._stream_uri_full = self._build_stream_uri()
        self._init_started_at = 0.0

        self._stream_thread = threading.Thread(target=self._run_stream, name="ld-mobile-stream", daemon=True)
        self._stream_thread.start()

        if start_wait > 0:
            self._ready.wait(start_wait)

    # ---- public API ------------------------------------------------------

    def is_initialized(self) -> bool:
        return self._ready.is_set() and self._store.initialized

    def variation(self, key: str, default: Any) -> Any:
        with _eval_latency_metric("variation"):
            detail, _ = self._evaluate(key, default, self._event_factory_default)
        return detail["value"]

    def variation_detail(self, key: str, default: Any) -> dict[str, Any]:
        with _eval_latency_metric("variation_detail"):
            detail, _ = self._evaluate(key, default, self._event_factory_with_reasons)
        return detail

    def all_flags_state(self) -> _AllFlagsState:
        return _AllFlagsState(self._store.all())

    def identify(self, context: Context) -> None:
        if context is None or not context.valid:
            raise ValueError("identify() requires a valid context")
        if self._event_processor is not None:
            self._event_processor.send_event(self._event_factory_default.new_identify_event(context))
        self._reconnect_with_context(context)

    def track(self, event_name: str, data: Any = None, metric_value: float | None = None) -> None:
        if self._event_processor is None:
            return
        evt = self._event_factory_default.new_custom_event(event_name, self._context, data, metric_value)
        self._event_processor.send_event(evt)

    def flush(self) -> None:
        if self._event_processor is not None:
            self._event_processor.flush()

    def close(self, *, stop_events: bool = True) -> None:
        self._closed_flag.set()
        if self._sse is not None:
            try:
                self._sse.close()
            except Exception:
                pass
        if stop_events and self._event_processor is not None:
            try:
                self._event_processor.stop()
            except Exception:
                log.debug("event processor stop raised", exc_info=True)

    # ---- evaluation ------------------------------------------------------

    def _evaluate(self, key: str, default: Any, factory: EventFactory):
        flag_dict = self._store.get(key)
        if flag_dict is None:
            reason = {"kind": "ERROR", "errorKind": "FLAG_NOT_FOUND"}
            detail = {"value": default, "variationIndex": None, "reason": reason}
            if self._event_processor is not None:
                self._event_processor.send_event(factory.new_unknown_flag_event(key, self._context, default, reason))
            return detail, None

        flag = _FlagProxy(flag_dict)
        value = flag_dict.get("value", default)
        variation = flag_dict.get("variation")
        reason = flag_dict.get("reason")

        # The upstream evaluator can't run against mobile flag-state payloads
        # (no rules/segments), so we hand-roll an EventInputEvaluation. The
        # rest - summary aggregation, feature/debug gating, sampling, output
        # formatting - happens downstream in the event processor.
        track_events = flag.track_events or (factory.is_experiment(flag, reason) if reason else False)
        if self._event_processor is not None:
            include_reason = factory._with_reasons or track_events  # type: ignore[attr-defined]
            event = EventInputEvaluation(
                _now_ms(),
                self._context,
                key,
                flag,
                variation,
                value,
                reason if include_reason else None,
                default,
                None,
                track_events,
            )
            self._event_processor.send_event(event)

        return {"value": value, "variationIndex": variation, "reason": reason}, flag

    # ---- streaming -------------------------------------------------------

    def _build_stream_uri(self) -> str:
        uri = f"{self._stream_uri}{MEVAL_PATH}/{_context_b64(self._context)}"
        if self._evaluation_reasons:
            uri += "?" + urlencode({"withReasons": "true"})
        return uri

    def _reconnect_with_context(self, context: Context) -> None:
        self._closed_flag.set()
        if self._sse is not None:
            try:
                self._sse.close()
            except Exception:
                pass
        self._closed_flag = threading.Event()
        self._ready = threading.Event()
        self._context = context
        self._stream_uri_full = self._build_stream_uri()
        self._stream_thread = threading.Thread(target=self._run_stream, name="ld-mobile-stream", daemon=True)
        self._stream_thread.start()

    def _run_stream(self) -> None:
        log.info("starting mobile stream: %s", clean_name("GET", self._stream_uri_full))
        pool = create_http_pool_manager(
            num_pools=1,
            verify_ssl=self._verify_ssl,
            target_base_uri=self._stream_uri_full,
            force_proxy=self._http_proxy,
        )
        self._init_started_at = time.time()
        self._sse = SSEClient(
            connect=ConnectStrategy.http(
                url=self._stream_uri_full,
                headers=mobile_headers(self._sdk_key),
                pool=pool,
                urllib3_request_options={
                    "timeout": urllib3.Timeout(connect=DEFAULT_CONNECT_TIMEOUT, read=STREAM_READ_TIMEOUT),
                },
            ),
            error_strategy=ErrorStrategy.always_continue(),
            initial_retry_delay=self._initial_reconnect_delay,
            retry_delay_strategy=RetryDelayStrategy.default(max_delay=30, backoff_multiplier=2, jitter_multiplier=0.5),
            retry_delay_reset_threshold=60,
            logger=log,
        )
        try:
            for action in self._sse.all:
                if self._closed_flag.is_set():
                    break
                if isinstance(action, Start):
                    self._on_stream_start(failed=False)
                elif isinstance(action, Event):
                    self._handle_event(action)
                elif isinstance(action, Fault) and action.error is not None:
                    log.warning("mobile stream fault: %s", action.error)
                    self._on_stream_start(failed=True)
                    self._init_started_at = time.time()
        except Exception:
            log.exception("unexpected error in mobile stream loop")
        finally:
            try:
                if self._sse is not None:
                    self._sse.close()
            except Exception:
                pass

    def _on_stream_start(self, *, failed: bool) -> None:
        if self._diagnostic_accumulator is not None and self._init_started_at > 0:
            duration_ms = int((time.time() - self._init_started_at) * 1000)
            self._diagnostic_accumulator.record_stream_init(_now_ms(), duration_ms, failed)

    def _handle_event(self, msg: Event) -> None:
        if msg.event == "put":
            data = json.loads(msg.data) if msg.data else {}
            self._store.init(data)
            self._mark_ready(len(msg.data or ""))
        elif msg.event == "patch":
            payload = json.loads(msg.data)
            self._store.upsert(payload)
            self._record_flag_update_metric(payload)
        elif msg.event == "delete":
            payload = json.loads(msg.data)
            key = payload.get("key")
            version = int(payload.get("version", 0))
            if key:
                self._store.delete(key, version)
        elif msg.event == "ping":
            try:
                data = self._requester.get_all(self._context)
                self._store.init(data)
                self._mark_ready(0)
            except Exception:
                log.exception("ping fetch failed")
        else:
            log.debug("unhandled mobile stream event: %s", msg.event)

    def _mark_ready(self, payload_size: int) -> None:
        if self._ready.is_set():
            return
        locust_events.request.fire(
            request_type="ld:init",
            name="mobile",
            response_time=int((time.time() - self._init_started_at) * 1000),
            response_length=payload_size,
            response=None,
            exception=None,
            context={},
            url=self._stream_uri_full,
        )
        self._ready.set()

    @staticmethod
    def _record_flag_update_metric(payload: dict[str, Any]) -> None:
        if payload.get("key") != HEARTBEAT_FLAG_KEY:
            return
        try:
            value = int(payload.get("value") or 0)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        duration = int(time.time() * 1000 - value)
        locust_events.request.fire(
            request_type="sse:flag-update",
            name="mobile",
            response_time=duration,
            response_length=0,
            response=None,
            exception=None,
            context={},
            url="/meval",
        )


class _AllFlagsState:
    def __init__(self, flags: dict[str, dict[str, Any]]):
        self._flags = flags

    @property
    def valid(self) -> bool:
        return True

    def to_values_map(self) -> dict[str, Any]:
        return {k: v.get("value") for k, v in self._flags.items()}


# ---------------------------------------------------------------------------
# Locust user
# ---------------------------------------------------------------------------


class LaunchDarklyMobileLocust(LaunchDarklyLocust):
    """Mobile/client-side LaunchDarkly user emulator (uses :class:`MobileLDClient`)."""

    abstract = True

    sdk_key: str | None = os.environ.get("LAUNCHDARKLY_MOBILE_KEY")
    stream_uri: str | None = os.environ.get("LAUNCHDARKLY_MOBILE_STREAM_URI")

    def make_client(self) -> MobileLDClient:  # type: ignore[override]
        return MobileLDClient(
            context=self.context,
            sdk_key=self.sdk_key or "",
            base_uri=self.base_uri or "https://clientsdk.launchdarkly.com",
            stream_uri=self.stream_uri or "https://clientstream.launchdarkly.com",
            events_uri=self.events_uri or "https://mobile.launchdarkly.com",
            send_events=self.send_events,
        )


__all__ = ["LaunchDarklyMobileLocust", "MobileConfig", "MobileLDClient"]
