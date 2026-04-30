"""Server-side LaunchDarkly user emulator.

Wraps the real ``ldclient.LDClient`` and plugs in three drop-in component
replacements that route their HTTP through our locust-instrumented pool:

- :class:`LocustEventDispatcher` - Locust-aware ``DefaultEventProcessor``
- :class:`LocustFeatureRequester` - Locust-aware polling feature requester
- :class:`LocustStreamingProcessor` - Locust-aware streaming update processor
  that also emits an ``sse:flag-update`` metric whenever a patch on the
  ``locust-heartbeat`` flag is observed (used to time flag propagation).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from typing import Any

from ld_eventsource import SSEClient
from ld_eventsource.actions import Event
from ld_eventsource.config import (
    ConnectStrategy,
    ErrorStrategy,
    RetryDelayStrategy,
)
from ldclient import Config, Context, LDClient
from ldclient.evaluation import EvaluationDetail
from ldclient.hook import EvaluationSeriesContext, Hook, Metadata
from ldclient.impl.datasource.feature_requester import (
    FeatureRequesterImpl as _UpstreamFeatureRequester,
)
from ldclient.impl.datasource.streaming import (
    BACKOFF_RESET_INTERVAL,
    JITTER_RATIO,
    MAX_RETRY_DELAY,
    stream_read_timeout,
)
from ldclient.impl.datasource.streaming import (
    StreamingUpdateProcessor as _UpstreamStreaming,
)
from ldclient.impl.events.diagnostics import (
    _DiagnosticAccumulator,
    create_diagnostic_id,
)
from ldclient.impl.events.event_processor import DefaultEventProcessor
from ldclient.impl.util import log as _sdk_log
from ldclient.versioned_data_kind import FEATURES

from locust import User, events

from .transport import LocustHTTPFactory, ld_headers

log = logging.getLogger(__name__)

HEARTBEAT_FLAG_KEY = "locust-heartbeat"


# ---------------------------------------------------------------------------
# Component replacements
# ---------------------------------------------------------------------------


class LocustEventDispatcher(DefaultEventProcessor):
    """``DefaultEventProcessor`` whose HTTP pool fires Locust ``request`` events.

    Also auto-creates a ``_DiagnosticAccumulator`` when one isn't supplied and
    ``diagnostic_opt_out`` is False. This matches what the LDClient does for
    its own built-in dispatcher; LDClient *doesn't* do it for user-supplied
    ``event_processor_class``, so without this we'd silently lose the
    ``diagnostic-init`` and periodic ``diagnostic`` events on the server side.
    """

    def __init__(self, config, diagnostic_accumulator=None, dispatcher_class=None):
        http = LocustHTTPFactory(ld_headers(config.sdk_key), config.http).create_pool_manager(1, config.events_uri)
        if diagnostic_accumulator is None and not config.diagnostic_opt_out:
            diagnostic_accumulator = _DiagnosticAccumulator(create_diagnostic_id(config))
        super().__init__(
            config,
            http=http,
            dispatcher_class=dispatcher_class,
            diagnostic_accumulator=diagnostic_accumulator,
        )


class LocustFeatureRequester(_UpstreamFeatureRequester):
    """Polling-mode feature requester that uses our instrumented pool."""

    def __init__(self, config):
        super().__init__(config)
        self._http = LocustHTTPFactory(ld_headers(config.sdk_key), config.http).create_pool_manager(1, config.base_uri)


class LocustStreamingProcessor(_UpstreamStreaming):
    """Streaming update processor that:

    - routes the SSE connection through the locust-instrumented HTTP pool, so
      stream connect/disconnect/heartbeat traffic shows in stats
    - emits an ``sse:flag-update`` Locust request every time the
      ``locust-heartbeat`` flag is patched, measuring server -> client
      propagation latency
    """

    def __init__(self, config, store, ready):
        # Upstream signature is (config, store, ready, diagnostic_accumulator);
        # the SDK invokes user-supplied update_processor_class with three args.
        super().__init__(config, store, ready, None)

    def _create_sse_client(self) -> SSEClient:
        factory = LocustHTTPFactory(
            ld_headers(self._config.sdk_key),
            self._config.http,
            override_read_timeout=stream_read_timeout,
        )
        return SSEClient(
            connect=ConnectStrategy.http(
                url=self._uri,
                headers=factory.base_headers,
                pool=factory.create_pool_manager(1, self._uri),
                urllib3_request_options={"timeout": factory.timeout},
            ),
            error_strategy=ErrorStrategy.always_continue(),
            initial_retry_delay=self._config.initial_reconnect_delay,
            retry_delay_strategy=RetryDelayStrategy.default(
                max_delay=MAX_RETRY_DELAY,
                backoff_multiplier=2,
                jitter_multiplier=JITTER_RATIO,
            ),
            retry_delay_reset_threshold=BACKOFF_RESET_INTERVAL,
            logger=_sdk_log,
        )

    def _process_message(self, store, msg: Event) -> bool:
        result = super()._process_message(store, msg)
        # Initialization-latency metric: fired the first time a `put` succeeds.
        # `_ready` is set by upstream *after* this method returns when result
        # is True, so a False there means we're still in the initial-init
        # window. ``_connection_attempt_start_time`` is set by upstream just
        # before iterating SSE actions.
        if result and not self._ready.is_set() and self._connection_attempt_start_time is not None:
            duration = int((time.time() - self._connection_attempt_start_time) * 1000)
            events.request.fire(
                request_type="ld:init",
                name="server",
                response_time=duration,
                response_length=0,
                response=None,
                exception=None,
                context={},
                url=self._uri,
            )
        if msg.event == "patch":
            try:
                payload = json.loads(msg.data)
                obj = payload.get("data") or {}
                path = payload.get("path") or ""
                if path == FEATURES.stream_api_path + HEARTBEAT_FLAG_KEY:
                    sent = obj.get("variations", [None])[0]
                    if isinstance(sent, (int, float)):
                        duration = int(time.time() * 1000 - sent)
                        events.request.fire(
                            request_type="sse:flag-update",
                            name="server",
                            response_time=duration,
                            response_length=0,
                            response=None,
                            exception=None,
                            context={},
                            url=self._uri,
                        )
            except Exception:
                log.debug("failed to record server-side heartbeat metric", exc_info=True)
        return result


# ---------------------------------------------------------------------------
# Evaluation-latency hook (server-side; relevant especially in daemon mode)
# ---------------------------------------------------------------------------


class _EvalLatencyHook(Hook):
    """Records every flag evaluation as an ``ld:eval`` Locust metric."""

    @property
    def metadata(self) -> Metadata:
        return Metadata(name="locust-eval-latency")

    def before_evaluation(self, series_context: EvaluationSeriesContext, data: dict) -> dict:
        return {**data, "_t0": time.perf_counter()}

    def after_evaluation(self, series_context: EvaluationSeriesContext, data: dict, detail: EvaluationDetail) -> dict:
        t0 = data.get("_t0")
        if t0 is not None:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            reason = detail.reason or {}
            exc: Exception | None = None
            if reason.get("kind") == "ERROR":
                exc = RuntimeError(reason.get("errorKind") or "unknown")
            events.request.fire(
                request_type="ld:eval",
                name=f"server.{series_context.method}",
                response_time=duration_ms,
                response_length=0,
                response=None,
                exception=exc,
                context={},
                url=series_context.key,
            )
        return data


# ---------------------------------------------------------------------------
# Locust user base
# ---------------------------------------------------------------------------


def _anonymous_context() -> Context:
    return Context.builder("anonymous").anonymous(True).build()


class LaunchDarklyLocust(User):
    """Server-side LaunchDarkly user emulator.

    Subclass and assign one or more task sets to ``tasks``. Override
    :meth:`generate_context` to produce per-user contexts, and use
    ``config_overrides`` to pass extra args to the underlying ``Config``.
    """

    abstract = True

    sdk_key: str | None = os.environ.get("LAUNCHDARKLY_SDK_KEY")
    base_uri: str | None = os.environ.get("LAUNCHDARKLY_BASE_URI")
    events_uri: str | None = os.environ.get("LAUNCHDARKLY_EVENTS_URI")
    stream_uri: str | None = os.environ.get("LAUNCHDARKLY_STREAM_URI")
    # https://docs.launchdarkly.com/guides/flags/testing-code#managing-test-data-in-your-production-environment
    send_events: bool = False
    config_overrides: dict = {}

    def __init__(self, environment) -> None:
        super().__init__(environment)
        self._ldclient: LDClient | None = None
        self._context: Context | None = None
        if not self.base_uri:
            self.base_uri = self.host
        if not self.events_uri:
            self.events_uri = self.host
        if not self.stream_uri:
            self.stream_uri = self.host

    def generate_context(self) -> Context:
        return _anonymous_context()

    @property
    def context(self) -> Context:
        if self._context is None:
            self._context = self.generate_context()
        return self._context

    # Backwards-compatible alias for the legacy attribute name.
    @property
    def user(self) -> Context:
        return self.context

    def _build_config(self) -> Config:
        kwargs: dict[str, Any] = dict(
            sdk_key=self.sdk_key or "",
            base_uri=self.base_uri or "https://app.launchdarkly.com",
            events_uri=self.events_uri or "https://events.launchdarkly.com",
            stream_uri=self.stream_uri or "https://stream.launchdarkly.com",
            send_events=self.send_events,
            update_processor_class=LocustStreamingProcessor,
            event_processor_class=LocustEventDispatcher,
            feature_requester_class=LocustFeatureRequester,
            hooks=[_EvalLatencyHook()],
        )
        kwargs.update(copy.deepcopy(self.config_overrides))
        return Config(**kwargs)

    def make_client(self) -> LDClient:
        return LDClient(config=self._build_config())

    @property
    def ldclient(self) -> LDClient:
        if self._ldclient is None:
            self._ldclient = self.make_client()
        return self._ldclient

    def close_client(self) -> None:
        try:
            if self._ldclient is not None:
                self._ldclient.close()
        finally:
            self._ldclient = None


__all__ = [
    "LaunchDarklyLocust",
    "LocustEventDispatcher",
    "LocustFeatureRequester",
    "LocustStreamingProcessor",
]
