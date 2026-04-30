"""Flag-propagation timing instrumentation (opt-in).

Given a ``locust-heartbeat`` flag and an API key that can update it, the
master process PATCHes the flag's variations every 30s with the current epoch
millis. Each emulated client subtracts that timestamp from the wire-arrival
time and emits the delta as an ``sse:flag-update`` row, measuring
LaunchDarkly → Relay → client propagation.

Wire it up in your locustfile::

    from ld_locust import enable_flag_propagation_metric
    enable_flag_propagation_metric()
"""

from __future__ import annotations

import json
import logging
import os
import time

import gevent
from locust.runners import LocalRunner, MasterRunner

from locust import events

from .transport import create_http_pool_manager

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30


DEFAULT_API_BASE = "https://app.launchdarkly.com"


def enable_flag_propagation_metric(
    *,
    project_env: str = "LAUNCHDARKLY_HEARTBEAT_PROJECT",
    api_key_env: str = "LAUNCHDARKLY_HEARTBEAT_API_KEY",
    api_base_env: str = "LAUNCHDARKLY_API_BASE",
    flag_key: str = "locust-heartbeat",
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    api_base: str | None = None,
) -> None:
    """Register a Locust ``init`` listener that runs the heartbeat thread on master.

    The PATCH endpoint resolves in this order: explicit ``api_base`` argument,
    then the ``LAUNCHDARKLY_API_BASE`` env var (override ``api_base_env`` to
    rename it), then ``https://app.launchdarkly.com``. Set the env var to
    ``https://app.launchdarkly.us`` for federal, or to your EU region URL.
    """

    @events.init.add_listener
    def _on_init(environment, **_kwargs):  # noqa: ANN001
        runner = environment.runner
        if not isinstance(runner, (LocalRunner, MasterRunner)):
            return
        proj = os.environ.get(project_env)
        api_key = os.environ.get(api_key_env)
        if not proj:
            log.warning("%s unset; flag-propagation metric disabled", project_env)
            return
        if not api_key:
            log.warning("%s unset; flag-propagation metric disabled", api_key_env)
            return
        resolved_api_base = api_base or os.environ.get(api_base_env) or DEFAULT_API_BASE
        gevent.spawn(_run_heartbeat, runner, proj, api_key, flag_key, interval_seconds, resolved_api_base)


def _run_heartbeat(runner, project: str, api_key: str, flag_key: str, interval_seconds: int, api_base: str) -> None:
    update_endpoint = f"{api_base}/api/v2/flags/{project}/{flag_key}"
    pool = create_http_pool_manager(num_pools=1, verify_ssl=True, target_base_uri=api_base)
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json-patch+json",
        "Accept": "application/json",
    }
    while runner.state != "stopped":
        now_ms = int(time.time() * 1000)
        body = json.dumps(
            [
                {"op": "replace", "path": "/variations/0/value", "value": now_ms},
                {"op": "replace", "path": "/variations/1/value", "value": now_ms + 1},
            ]
        ).encode("utf-8")
        try:
            pool.request("PATCH", update_endpoint, headers=headers, body=body)
        except Exception as exc:
            log.error("failed to update %s: %s", flag_key, exc)
        gevent.sleep(interval_seconds)


__all__ = ["enable_flag_propagation_metric"]
