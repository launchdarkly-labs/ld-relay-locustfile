"""Locust-instrumented FDv2 data sources ("data saving mode").

The FDv2 data system replaces the FDv1 update processor and feature requester
with *initializers* (one-shot fetches) and *synchronizers* (long-lived
connections), selected through ``Config(datasystem_config=...)``. Once that
option is set the SDK ignores ``update_processor_class`` and
``feature_requester_class``, so the FDv1 components in :mod:`ld_locust.server`
never run and every metric they report disappears.

This module wraps the SDK's own FDv2 data sources to put those metrics back:

- requests go through the Locust-instrumented pool, so ``sse:connect`` and
  ``GET`` rows still show up per endpoint,
- the first payload to land - from the polling initializer or the streaming
  synchronizer, whichever wins - is recorded once as ``ld:init``,
- ``locust-heartbeat`` changes arriving over a stream are timed as
  ``sse:flag-update``.

The presets mirror :mod:`ldclient.datasystem`: :func:`default`,
:func:`streaming`, :func:`polling`, and :func:`custom`. Importing this module
instruments the SDK's FDv2 HTTP pools process-wide; see
:func:`instrument_http`.

Data saving mode is an Early Access feature. It needs the Python SDK 9.13 or
later and Relay Proxy 9.0.0-rc.1 or later.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from typing import Any

from ldclient import datasystem as ld_datasystem
from ldclient.config import DataSourceBuilderConfig, HTTPConfig
from ldclient.datasystem import ConfigBuilder
from ldclient.impl.datasourcev2 import polling as _polling_module
from ldclient.impl.datasourcev2 import streaming as _streaming_module
from ldclient.interfaces import ChangeSet, IntentCode, ObjectKind, SelectorStore, Update

from locust import events

from .transport import LocustHTTPFactory

log = logging.getLogger(__name__)

HEARTBEAT_FLAG_KEY = "locust-heartbeat"

# Locust stat row name, shared with the FDv1 components in server.py so both
# modes report into the same rows.
STAT_NAME = "server"

# Endpoint paths, for labelling stat rows only. These are relay/LaunchDarkly
# wire paths rather than SDK internals, so they are spelled out here instead of
# imported from ldclient.impl.
STREAMING_ENDPOINT = "/sdk/stream"
POLLING_ENDPOINT = "/sdk/poll"
FDV1_POLLING_ENDPOINT = "/sdk/latest-all"


# ---------------------------------------------------------------------------
# HTTP instrumentation
# ---------------------------------------------------------------------------

_instrumented = False


def instrument_http() -> None:
    """Route the SDK's FDv2 data sources through the Locust-instrumented pool.

    Those data sources build their pool internally - inside the SDK's own SSE
    client factory and polling requesters - with no seam to pass one in, so we
    rebind the ``HTTPFactory`` name their modules resolve.
    :class:`~ld_locust.transport.LocustHTTPFactory` is a subclass that only
    overrides ``create_pool_manager``, so nothing changes except that each call
    fires a Locust ``request`` event.

    Idempotent, and called on import of this module. Every FDv2 client in this
    process is a load-test client, so patching process-wide is intentional.
    """
    global _instrumented
    if _instrumented:
        return
    _instrumented = True
    for module in (_streaming_module, _polling_module):
        if not hasattr(module, "HTTPFactory"):
            log.warning(
                "%s no longer builds its pool through HTTPFactory; per-endpoint stat rows will be missing",
                module.__name__,
            )
            continue
        module.HTTPFactory = LocustHTTPFactory


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class InitMetric:
    """One-shot ``ld:init`` timer shared by every data source of one client.

    The data system rebuilds its sources whenever it rotates synchronizers, and
    either the initializer or the synchronizer can produce the first payload.
    Sharing one instance keeps each client to a single ``ld:init`` sample - the
    time to first usable flag data - rather than one per source.
    """

    def __init__(self) -> None:
        self._recorded = False

    def record(self, started_at: float, url: str) -> None:
        """Record the payload that started at ``started_at``, unless already done."""
        if self._recorded:
            return
        self._recorded = True
        events.request.fire(
            request_type="ld:init",
            name=STAT_NAME,
            response_time=int((time.time() - started_at) * 1000),
            response_length=0,
            response=None,
            exception=None,
            context={},
            url=url,
        )


class LocustDataSource:
    """Delegating wrapper that reports an FDv2 data source's payloads to Locust.

    Works for both roles: ``fetch`` is the initializer entry point, ``sync`` the
    synchronizer one. Everything it measures comes off the public ``Basis`` and
    ``Update`` objects the wrapped source produces, so it doesn't reach into the
    data source itself.
    """

    def __init__(self, inner: Any, url: str, init_metric: InitMetric, *, heartbeats: bool = False) -> None:
        self._inner = inner
        self._url = url
        self._init_metric = init_metric
        self._heartbeats = heartbeats

    @property
    def name(self) -> str:
        """The wrapped source's name, used in the SDK's own logging."""
        return self._inner.name

    def set_diagnostic_accumulator(self, diagnostic_accumulator: Any) -> None:
        """Forward the accumulator when the wrapped source collects diagnostics."""
        setter = getattr(self._inner, "set_diagnostic_accumulator", None)
        if setter is not None:
            setter(diagnostic_accumulator)

    def fetch(self, ss: SelectorStore) -> Any:
        """Initializer entry point: one fetch of the current payload."""
        started_at = time.time()
        result = self._inner.fetch(ss)
        # A _Success carries a Basis; a _Fail has no `value` and no change set.
        basis = getattr(result, "value", None)
        self._observe(getattr(basis, "change_set", None), started_at)
        return result

    def sync(self, ss: SelectorStore) -> Generator[Update, None, None]:
        """Synchronizer entry point: yields updates until the source stops."""
        started_at = time.time()
        for update in self._inner.sync(ss):
            self._observe(update.change_set, started_at)
            yield update

    def stop(self) -> None:
        """Stop the wrapped source."""
        self._inner.stop()

    def _observe(self, change_set: ChangeSet | None, started_at: float) -> None:
        # TRANSFER_NONE means the data we already hold is current: no payload
        # arrived, so this is not the moment the client became usable.
        if change_set is None or change_set.intent_code == IntentCode.TRANSFER_NONE:
            return
        self._init_metric.record(started_at, self._url)
        # Only time heartbeats carried by a change transfer. A full transfer -
        # the initial payload, or a reconnect without a basis - replays the
        # heartbeat flag holding whatever timestamp it last had, which would
        # register as an enormous propagation delay.
        if self._heartbeats and change_set.intent_code == IntentCode.TRANSFER_CHANGES:
            self._record_heartbeats(change_set)

    def _record_heartbeats(self, change_set: ChangeSet) -> None:
        try:
            for change in change_set.changes:
                if change.kind != ObjectKind.FLAG or change.key != HEARTBEAT_FLAG_KEY:
                    continue
                sent = ((change.object or {}).get("variations") or [None])[0]
                if not isinstance(sent, (int, float)):
                    continue
                duration = int(time.time() * 1000 - sent)
                events.request.fire(
                    request_type="sse:flag-update",
                    name=STAT_NAME,
                    response_time=duration,
                    response_length=0,
                    response=None,
                    exception=None,
                    context={},
                    url=self._url,
                )
        except Exception:
            log.debug("failed to record server-side heartbeat metric", exc_info=True)


# ---------------------------------------------------------------------------
# Data source builders
# ---------------------------------------------------------------------------


class LocustDataSourceBuilder:
    """Wraps an upstream FDv2 data source builder in Locust instrumentation.

    Exposes the same fluent setters as the builders in
    :mod:`ldclient.datasystem`, so instances drop straight into
    ``initializers()`` and ``synchronizers()``.
    """

    def __init__(
        self,
        inner: Any,
        endpoint: str,
        init_metric: InitMetric | None = None,
        *,
        heartbeats: bool = False,
    ) -> None:
        self._inner = inner
        self._endpoint = endpoint
        self._init_metric = init_metric or InitMetric()
        self._heartbeats = heartbeats
        self._base_uri: str | None = None

    def base_uri(self, uri: str) -> LocustDataSourceBuilder:
        """Point this data source at a specific host, e.g. a relay."""
        self._base_uri = uri.rstrip("/")
        self._inner.base_uri(uri)
        return self

    def http_options(self, http_options: HTTPConfig) -> LocustDataSourceBuilder:
        """Override the HTTP configuration for this data source."""
        self._inner.http_options(http_options)
        return self

    def poll_interval(self, poll_interval: float) -> LocustDataSourceBuilder:
        """Set the interval between polls (polling sources only)."""
        self._inner.poll_interval(poll_interval)
        return self

    def initial_reconnect_delay(self, delay: float) -> LocustDataSourceBuilder:
        """Set the initial stream reconnect delay (streaming sources only)."""
        self._inner.initial_reconnect_delay(delay)
        return self

    def build(self, config: DataSourceBuilderConfig) -> LocustDataSource:
        """Build the wrapped data source, filling unset options from ``config``."""
        instrument_http()
        default_base = config.stream_base_uri if self._endpoint == STREAMING_ENDPOINT else config.base_uri
        url = (self._base_uri or default_base) + self._endpoint
        return LocustDataSource(
            self._inner.build(config),
            url,
            self._init_metric,
            heartbeats=self._heartbeats,
        )


def streaming_ds_builder(init_metric: InitMetric | None = None) -> LocustDataSourceBuilder:
    """Return a builder for an instrumented streaming synchronizer."""
    return LocustDataSourceBuilder(
        ld_datasystem.streaming_ds_builder(),
        STREAMING_ENDPOINT,
        init_metric,
        heartbeats=True,
    )


def polling_ds_builder(init_metric: InitMetric | None = None) -> LocustDataSourceBuilder:
    """Return a builder for an instrumented polling initializer/synchronizer."""
    return LocustDataSourceBuilder(ld_datasystem.polling_ds_builder(), POLLING_ENDPOINT, init_metric)


def fdv1_fallback_ds_builder(init_metric: InitMetric | None = None) -> LocustDataSourceBuilder:
    """Return a builder for the instrumented FDv1-compatible fallback synchronizer.

    The data system switches to this one when the environment does not serve
    FDv2 - it polls the FDv1 endpoint and converts the payload into a change set.
    """
    return LocustDataSourceBuilder(ld_datasystem.fdv1_fallback_ds_builder(), FDV1_POLLING_ENDPOINT, init_metric)


# ---------------------------------------------------------------------------
# Presets (mirrors ldclient.datasystem)
# ---------------------------------------------------------------------------


def custom() -> ConfigBuilder:
    """Return an empty data system builder to assemble by hand.

    Pair it with the ``*_ds_builder`` functions above, sharing one
    :class:`InitMetric` across every source so each client reports a single
    ``ld:init``.
    """
    return ConfigBuilder()


def default(init_metric: InitMetric | None = None) -> ConfigBuilder:
    """Instrumented equivalent of :func:`ldclient.datasystem.default`.

    Initializes from a poll, then streams, falling back to polling when the
    stream is unavailable and to FDv1 when the environment does not serve FDv2.
    """
    init_metric = init_metric or InitMetric()
    poll_builder = polling_ds_builder(init_metric)

    builder = custom()
    builder.initializers([poll_builder])
    builder.synchronizers(streaming_ds_builder(init_metric), poll_builder)
    builder.fdv1_compatible_synchronizer(fdv1_fallback_ds_builder(init_metric))
    return builder


def streaming(init_metric: InitMetric | None = None) -> ConfigBuilder:
    """Instrumented equivalent of :func:`ldclient.datasystem.streaming`."""
    init_metric = init_metric or InitMetric()

    builder = custom()
    builder.synchronizers(streaming_ds_builder(init_metric))
    builder.fdv1_compatible_synchronizer(fdv1_fallback_ds_builder(init_metric))
    return builder


def polling(init_metric: InitMetric | None = None) -> ConfigBuilder:
    """Instrumented equivalent of :func:`ldclient.datasystem.polling`.

    No ``sse:flag-update`` rows in this mode: a poll-detected change carries the
    poll interval inside the measurement, which says little about the relay.
    """
    init_metric = init_metric or InitMetric()

    builder = custom()
    builder.synchronizers(polling_ds_builder(init_metric))
    builder.fdv1_compatible_synchronizer(fdv1_fallback_ds_builder(init_metric))
    return builder


PRESETS = {
    "default": default,
    "streaming": streaming,
    "polling": polling,
}


def preset(name: str) -> ConfigBuilder:
    """Return the data system builder for a preset name.

    :param name: ``default``, ``streaming``, or ``polling``.
    :raises ValueError: if ``name`` is not a known preset.
    """
    factory = PRESETS.get(name)
    if factory is None:
        raise ValueError(f"unknown data system preset {name!r}; expected one of {', '.join(sorted(PRESETS))}")
    return factory()


instrument_http()


__all__ = [
    "HEARTBEAT_FLAG_KEY",
    "PRESETS",
    "InitMetric",
    "LocustDataSource",
    "LocustDataSourceBuilder",
    "custom",
    "default",
    "fdv1_fallback_ds_builder",
    "instrument_http",
    "polling",
    "polling_ds_builder",
    "preset",
    "streaming",
    "streaming_ds_builder",
]
