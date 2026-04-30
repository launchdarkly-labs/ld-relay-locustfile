"""Locust scenario for load-testing a LaunchDarkly Relay Proxy.

Adjust the task sets, weights, and ``send_events`` toggles below to match the
workload you want to emulate. The plumbing - SDK clients, mobile emulator,
HTTP instrumentation, flag-propagation timing - lives in the ``ld_locust``
package alongside this file.
"""

from __future__ import annotations

import json
import logging
import os
import random

from ld_locust import (
    LaunchDarklyLocust,
    LaunchDarklyMobileLocust,
    enable_flag_propagation_metric,
)
from ldclient import Context

from locust import SequentialTaskSet, TaskSet, between, task

log = logging.getLogger(__name__)


# Optional JSON config file for terraform/cloud-init provisioning. Existing
# env vars take precedence (`setdefault` semantics).
try:
    with open(os.environ.get("LD_ENV_FILE", "/etc/ldlocust.json")) as fp:
        for k, v in json.load(fp).items():
            os.environ.setdefault(k, str(v))
except (OSError, ValueError):
    pass


# --- Mobile workload ---------------------------------------------------------


class LaunchDarklyMobileTaskSet(SequentialTaskSet):
    wait_time = between(1, 30)

    @task
    def init_ldclient(self) -> None:
        if not self.user.ldclient.is_initialized():
            self.user.close_client()
            raise RuntimeError("failed to initialize client")

    @task
    class LDMobileTasks(TaskSet):
        wait_time = between(1, 30)

        @task(10)
        def evaluate_flags(self) -> None:
            flags = self.user.ldclient.all_flags_state().to_values_map()
            if not flags:
                return
            flag, _ = random.choice(list(flags.items()))
            self.user.ldclient.variation(flag, None)

        @task(8)
        def reidentify(self) -> None:
            new_context = Context.builder("anonymous").anonymous(True).set("groups", ["admin"]).build()
            self.user.ldclient.identify(new_context)
            if not self.user.ldclient.is_initialized():
                self.user.close_client()

        @task(3)
        def track_event(self) -> None:
            self.user.ldclient.track("test_event", metric_value=1)

        @task(1)
        def done(self) -> None:
            self.interrupt()

    @task
    def disconnect(self) -> None:
        # Ends the session so the SequentialTaskSet loops back to init,
        # simulating intermittent connectivity.
        self.user.close_client()

    def on_stop(self) -> None:
        self.user.close_client()


# --- Server workload ---------------------------------------------------------


class LaunchDarklyServerTaskSet(SequentialTaskSet):
    wait_time = between(1, 30)

    @task
    def init_ldclient(self) -> None:
        if not self.user.ldclient.is_initialized():
            self.user.close_client()
            raise RuntimeError("failed to initialize client")

    @task
    class LDServerTasks(TaskSet):
        wait_time = between(1, 30)

        @task(10)
        def evaluate_flags(self) -> None:
            flags = self.user.ldclient.all_flags_state(self.user.context).to_values_map()
            if not flags:
                return
            flag, _ = random.choice(list(flags.items()))
            self.user.ldclient.variation(flag, self.user.context, None)

        @task(3)
        def track_event(self) -> None:
            self.user.ldclient.track("test_event", self.user.context, metric_value=1)

        @task(1)
        def done(self) -> None:
            self.interrupt()

    @task
    def disconnect(self) -> None:
        self.user.close_client()

    def on_stop(self) -> None:
        self.user.close_client()


# --- User mix ----------------------------------------------------------------


class LaunchDarklyBasicServer(LaunchDarklyLocust):
    tasks = [LaunchDarklyServerTaskSet]
    weight = 1
    # Set to False when pointing at a production project to keep synthetic
    # events out of your real analytics.
    send_events = True


class LaunchDarklyBasicMobile(LaunchDarklyMobileLocust):
    tasks = [LaunchDarklyMobileTaskSet]
    weight = 2
    send_events = True


# No-op unless LAUNCHDARKLY_HEARTBEAT_PROJECT and LAUNCHDARKLY_HEARTBEAT_API_KEY
# are set in the env.
enable_flag_propagation_metric()
