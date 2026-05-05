# Endpoints

This page describes the requests this load test produces and how they appear in the Locust stats panel. It does not document relay's internal behavior.

For the relay-side semantics, the relay's own endpoint reference is authoritative:

> **[launchdarkly/ld-relay/blob/v8/docs/endpoints.md](https://github.com/launchdarkly/ld-relay/blob/v8/docs/endpoints.md)**

## Streaming endpoints

### `sse:connect http://<relay>/all`

This table summarizes the request:

| Field | Value |
|---|---|
| SDK Type | Server SDK |
| Method | `GET`, long-lived SSE |
| Path | `/all` |
| Auth | `Authorization: <sdk_key>` |

This is the upstream LaunchDarkly server SDK's streaming connection. The path and auth header come from the SDK, not from this load test.

In Locust, this row counts **connection attempts**. A single user produces one `sse:connect` entry per reconnect.

### `sse:connect http://<relay>/meval/[context]`

This table summarizes the request:

| Field | Value |
|---|---|
| SDK Type | Mobile or client-side SDK |
| Method | `GET`, long-lived SSE |
| Path | `/meval/{base64(context)}` |
| Auth | `Authorization: api_key <mobile_key>` |

This is the initialization/update stream used by mobile clients in streaming mode. One connection will be created per SDK instance.

## Polling fallback

### `GET http://<relay>/msdk/evalx/contexts/[context]`

This table summarizes the request:

| Field | Value |
|---|---|
| SDK Type | Mobile or client-side SDK |
| Method | `GET` |
| Path | `/msdk/evalx/contexts/{base64(context)}` |
| Auth | `Authorization: api_key <mobile_key>` |

The mobile emulator polls this endpoint when it receives a `ping` event over its `/meval` stream. The path constant lives at [`EVALX_CONTEXTS_PATH` in `mobile.py`](../locust/ld_locust/mobile.py).

## Analytics events

### `POST http://<relay>/bulk`

This table summarizes the request:

| Field | Value |
|---|---|
| SDK Type | Server SDK |
| Method | `POST` |
| Path | `/bulk` |
| Auth | `Authorization: <sdk_key>` |
| Body | JSON array of analytics events |

Server SDK analytics events. The SDK will send these requests according the configured flush interval. See LDConfig documentation for details.

### `POST http://<relay>/diagnostic`

This table summarizes the request:

| Field | Value |
|---|---|
| SDK Type | Server SDK |
| Method | `POST` |
| Path | `/diagnostic` |

SDK self-diagnostic events. Emitted by the server SDK on startup and periodically thereafter.

### `POST http://<relay>/mobile/events/bulk`

This table summarizes the request:

| Field | Value |
|---|---|
| SDK Type | Mobile or client-side SDK |
| Method | `POST` |
| Path | `/mobile/events/bulk` |
| Auth | `Authorization: api_key <mobile_key>` |

Mobile sdk analytics events. The SDK will send these requests according the configured flush interval. See LDConfig documentation for details.

### `POST http://<relay>/mobile/events/diagnostic`

This table summarizes the request:

| Field | Value |
|---|---|
| SDK Type | Mobile or client-side SDK |
| Method | `POST` |
| Path | `/mobile/events/diagnostic` |

Mobile counterpart to `/diagnostic`. The default `diagnostic_recording_interval` is 15 minutes

## Out-of-band: the heartbeat PATCH

### `PATCH https://app.launchdarkly.com/api/v2/flags/<project>/locust-heartbeat`

This table summarizes the request:

| Field | Value |
|---|---|
| Method | `PATCH`, every 30s |
| Auth | `Authorization: <heartbeat_api_key>`. This token is separate from any SDK key. |

This is **not** SDK traffic. It is the load test itself, writing the current millisecond clock into the variations of the `locust-heartbeat` flag so the [Update Propagation Latency](metrics.md#3-update-propagation-latency) metric has something to measure. The PATCH targets LaunchDarkly's admin API directly. The implementation lives in [`heartbeat.py`](../locust/ld_locust/heartbeat.py).

If this row shows failures, the `locust-heartbeat` flag either does not exist or the API token does not have the right policy. The README's [Locust API token policy](../README.md#locust-api-token-policy) section walks through the fix.

## Diagnosing missing rows

This table maps a missing row to its likely cause:

| Row missing | Likely cause |
|---|---|
| `sse:connect /all` | No server users in the swarm. Check `LaunchDarklyBasicServer.weight`. |
| `sse:connect /meval/[context]` | No mobile users. Check `LaunchDarklyBasicMobile.weight`. |
| `POST /bulk`, `POST /mobile/events/bulk` | `send_events = False` on the User subclass, or the swarm has not run long enough to flush. |
| `POST /diagnostic`, `POST /mobile/events/diagnostic` | `diagnostic_opt_out = True` in your config, or the swarm tore down before the first flush. |
| `GET /msdk/evalx/contexts/[context]` | The mobile emulator has not received a `ping` event during the run. |
| `sse:flag-update server`, `sse:flag-update mobile` | The heartbeat thread has not fired yet. The first PATCH lands at roughly t=30s. The heartbeat token may also be missing permissions, in which case the `PATCH .../locust-heartbeat` row shows failures. |
| `ld:eval server.variation` | The task set has not reached its `evaluate_flags` task yet, or your `flush_interval` and wait times are squelching it. |

For more details see the [relay docs](https://github.com/launchdarkly/ld-relay/blob/v8/docs/endpoints.md) and the project [README](../README.md#additional-metrics).
