# Endpoints

This load test exercises the public LaunchDarkly Relay Proxy surface that real LaunchDarkly SDKs talk to. Each row below shows up in the Locust stats panel as the test runs; this doc explains what each row represents, which side of the SDK fleet (server vs. mobile/client) is responsible for it, and how relay handles the request internally.

The relay's own endpoint reference is the authoritative source for the relay-side semantics:

> **[launchdarkly/ld-relay/blob/v8/docs/endpoints.md](https://github.com/launchdarkly/ld-relay/blob/v8/docs/endpoints.md)**

This page is the load-test-side view: which SDK code paths produce which Locust rows, and what to look for when something goes wrong.

---

## Streaming endpoints

### `sse:connect http://<relay>/all`

| Side | Server SDK |
|---|---|
| Method | `GET` (long-lived SSE) |
| Path | `/all` |
| Auth | `Authorization: <sdk_key>` (raw key) |
| Frame | Full flag + segment definitions |

Server SDKs subscribe to `/all` to receive every flag and segment definition in the environment. Relay multiplexes this stream from a single upstream connection to LaunchDarkly. The `put` event on stream open contains the entire dataset; subsequent `patch` and `delete` events deliver incremental changes.

In Locust this row counts **connection attempts**, not the duration of the long-lived stream itself. A single user produces one `sse:connect` entry per (re)connection. Per-stream traffic shape (heartbeats, payload sizes) is not visible at the request level - look at relay's own metrics for that.

### `sse:connect http://<relay>/meval/[context]`

| Side | Mobile / client-side SDK |
|---|---|
| Method | `GET` (long-lived SSE) |
| Path | `/meval/{base64(context)}` |
| Auth | `Authorization: api_key <mobile_key>` |
| Frame | Pre-evaluated flag values for *this* context only |

Mobile SDKs subscribe to `/meval` per-context. The base64-encoded context is included in the path itself, which is why the Locust row name uses `[context]` as a placeholder - the [`clean_name()` helper](../locust/ld_locust/transport.py) strips the per-user payload so the rows aggregate.

Relay maintains a separate fan-out connection per unique context. When a flag patch arrives upstream, relay re-evaluates affected flags against every connected context and emits per-context patches. This is dramatically more upstream traffic than `/all` for the same number of clients, which is why the mobile path exists as a load shape worth measuring separately.

---

## Polling fallback

### `GET http://<relay>/msdk/evalx/contexts/[context]`

| Side | Mobile / client-side SDK |
|---|---|
| Method | `GET` |
| Path | `/msdk/evalx/contexts/{base64(context)}` |
| Auth | `Authorization: api_key <mobile_key>` |

When the mobile SDK receives a `ping` event over its stream (relay's signal that the SDK should re-fetch state instead of expecting a `put`), the SDK falls back to polling this endpoint for the full pre-evaluated flag set for the context.

In a healthy steady state most of these requests come from the *initial* connection only - relay typically delivers state via `put` over the stream, and `ping` is reserved for cases where relay's own state is being rebuilt or the per-context evaluation cache is cold.

A `REPORT` variant exists at `/msdk/evalx/context` (no `s`) where the body carries the context as JSON instead of base64-encoding it into the URL. Our emulator does not currently exercise the `REPORT` path; if you want to test it, set up the emulator with `useReport=true` and observe `REPORT` rows.

---

## Analytics events

### `POST http://<relay>/bulk`

| Side | Server SDK |
|---|---|
| Method | `POST` (every 5s by default) |
| Path | `/bulk` |
| Auth | `Authorization: <sdk_key>` |
| Body | `[event, event, ...]` JSON array |
| Headers | `X-LaunchDarkly-Event-Schema: 4`, `X-LaunchDarkly-Payload-ID: <uuid>`, optional `Content-Encoding: gzip` |

Server SDK analytics events. The payload contains a mix of:

- `feature` events (one per `variation*` call where `flag.trackEvents` is set)
- `debug` events (one per call while a flag is in its debug-events window)
- `index` events (one per unique context, for context indexing)
- `summary` events (one per flush, aggregating every `variation*` call into per-(variation, version) counters)
- `identify` events (from `LDClient.identify()`)
- `custom` events (from `LDClient.track()`)

Relay forwards these to LaunchDarkly's events ingest. The flush interval is whatever `flush_interval` is in the SDK config (5 seconds by default, server SDK).

### `POST http://<relay>/diagnostic`

| Side | Server SDK |
|---|---|
| Method | `POST` (once on startup, then every 15&nbsp;min by default) |
| Path | `/diagnostic` |

SDK self-diagnostic events. The first request is a `diagnostic-init` describing the SDK build, runtime, and configuration; subsequent ones are periodic `diagnostic` stats describing dropped events, stream-init durations, and similar counters.

These are the SDK telling LaunchDarkly about its own health, not application-level data. Useful for watching for SDK-side anomalies (event drops, stream reconnect storms) under load.

### `POST http://<relay>/mobile/events/bulk`

| Side | Mobile / client-side SDK |
|---|---|
| Method | `POST` (every 30s by default) |
| Path | `/mobile/events/bulk` |
| Auth | `Authorization: api_key <mobile_key>` |

Same shape as `/bulk` but for the mobile/client wire protocol. The default flush interval is **30 seconds** for mobile (vs. 5 seconds for server) - mobile SDKs prioritize battery life and bandwidth, so events batch much more aggressively.

The mobile event mix is also subtly different:

- **No** `index` events (mobile SDKs don't emit them - one client = one context)
- `feature` events use anonymous-attribute redaction on the embedded context
- `summary` events use the mobile-specific `(variation, versionForEvents)` keying where `versionForEvents = flagVersion ?? version`

### `POST http://<relay>/mobile/events/diagnostic`

| Side | Mobile / client-side SDK |
|---|---|
| Method | `POST` (once on startup, then every 15&nbsp;min by default) |
| Path | `/mobile/events/diagnostic` |

Mobile counterpart to `/diagnostic`. Same purpose; different URL.

---

## Out-of-band: the heartbeat PATCH

### `PATCH https://app.launchdarkly.com/api/v2/flags/<project>/locust-heartbeat`

| Side | Locust master only (one greenlet, regardless of user count) |
|---|---|
| Method | `PATCH` (every 30s) |
| Auth | `Authorization: <heartbeat_api_key>` (separate from SDK keys) |

This is **not** SDK traffic. It's the load test rig itself, writing the current millisecond clock into the variations of the `locust-heartbeat` flag so that the [Update Propagation Latency](metrics.md#3-update-propagation-latency) metric has something to measure. Each PATCH hits LaunchDarkly's admin API directly, not relay.

If this row shows failures, the `locust-heartbeat` flag either doesn't exist or the API token doesn't have the right policy. The README's [Locust API token policy](../README.md#locust-api-token-policy) section walks through the fix.

---

## When something doesn't show up

| Row missing | Likely cause |
|---|---|
| `sse:connect /all` | No server users in the swarm. Check `LaunchDarklyBasicServer.weight`. |
| `sse:connect /meval/[context]` | No mobile users. Check `LaunchDarklyBasicMobile.weight`. |
| `POST /bulk`, `POST /mobile/events/bulk` | `send_events = False` on the User subclass, or the swarm hasn't run long enough to flush (5s server, 30s mobile). |
| `POST /diagnostic`, `POST /mobile/events/diagnostic` | `diagnostic_opt_out = True` somewhere in your config, *or* the diagnostic-init is sent under "Dropped events" because the swarm tore down before the first flush. |
| `GET /msdk/evalx/contexts/[context]` | Relay always sends `put` instead of `ping` for your account. This is normal in most steady-state configurations. |
| `sse:flag-update server` / `sse:flag-update mobile` | The heartbeat thread hasn't fired yet (first PATCH is at t≈30s), or the heartbeat token is missing permissions (look for the `PATCH .../locust-heartbeat` row showing fails). |
| `ld:eval server.variation` | The task set hasn't reached its `evaluate_flags` task yet, or your `flush_interval`/wait times are squelching it. |

For deeper relay-side analysis (cache hit rates, upstream connection pool health, fan-out latency), use relay's own Prometheus metrics - see [its docs](https://github.com/launchdarkly/ld-relay/blob/v8/docs/endpoints.md) and the project [README](../README.md#additional-metrics).
