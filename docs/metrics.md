# Key metrics

This load test surfaces four LaunchDarkly-specific metrics into the standard Locust stats panel. Each row in the panel has the same shape Locust uses for HTTP requests (`# reqs`, `# fails`, `Med`, `95%`, `Avg`, `Min`, `Max`, `req/s`), so all of the usual aggregations (response-time charts, percentiles, failure breakdowns, distributed-mode aggregation, exporters like [`locust_exporter`](https://github.com/ilsken/locust_exporter)) work without further configuration.

Metrics are emitted separately for the **server** SDK and the **mobile** emulator, so you can compare relay performance against the two SDK families under the same load.

## 1. Throughput (`req/s`)

| Where | What |
|---|---|
| **Locust header** | `Total RPS` (and per-row `req/s` columns) |

Locust's headline number. Counts every request the load test issues - SSE connect attempts, polling fetches, event POSTs, the heartbeat PATCH, and every synthetic LD-specific metric below. Useful as the macro-level signal: if RPS drops while user count stays flat, something is blocking somewhere downstream.

## 2. Initialization Latency

| Where | What |
|---|---|
| `ld:init server` | Time from server `LDClient` construction to first successful `put` from the streaming endpoint. |
| `ld:init mobile` | Time from `MobileLDClient` construction to first successful `put` (or polling fallback after a `ping`). |

Measures how long an SDK spends in its "not yet initialized" window. While the SDK is in this state, `variation()` calls return defaults rather than real flag values, so high initialization latency directly translates to user-visible incorrect flag evaluations on app cold-start.

The clock starts when the client object is created and stops the moment the SDK transitions to `is_initialized() == True`. For the server SDK this is the round-trip `client → relay → SSE put`. For the mobile emulator it's `client → relay → SSE put` *or* `client → relay → SSE ping → relay → polling response` depending on how relay decides to fan out the initial state.

## 3. Update Propagation Latency

| Where | What |
|---|---|
| `sse:flag-update server` | Wall-clock delta from when LaunchDarkly accepts a flag-variation PATCH to when the server SDK observes the resulting `patch` event over its streaming connection. |
| `sse:flag-update mobile` | Same, for the mobile streaming endpoint (`/meval`). |

The measurement loop:

1. The Locust master spawns a background thread (`enable_flag_propagation_metric()` in [`ld_locust/heartbeat.py`](../locust/ld_locust/heartbeat.py)) that PATCHes the `locust-heartbeat` flag every 30 seconds, writing the current epoch-millisecond clock into the variation values.
2. LaunchDarkly accepts the PATCH and propagates it to the relay's upstream stream.
3. Relay forwards the patch to every connected SDK over its `/all` (server) and `/meval` (mobile) SSE streams.
4. Our instrumented SDKs read the embedded timestamp out of the patch, subtract from the receive-time clock, and emit that delta as the metric.

This is end-to-end propagation: it covers LD ingest, LD edge fan-out, network to relay, relay's own SSE multiplexing, network to client, and client-side stream parsing. **It is the right metric to track when measuring user impact of flag changes**, because it's the latency a real user experiences between "SDK admin clicks Save" and "production traffic sees the new value."

The `locust-heartbeat` flag and its bootstrap policy are documented in the project [README](../README.md#optional-configure-a-flag-heartbeat). Without that flag and its API token, this metric row simply doesn't appear (the test still runs).

## 4. Evaluation Latency

| Where | What |
|---|---|
| `ld:eval server.variation` / `ld:eval server.variation_detail` | Wall-clock duration of a single `LDClient.variation()` / `variation_detail()` call. |
| `ld:eval mobile.variation` / `ld:eval mobile.variation_detail` | Same, for the mobile emulator. |

Mostly relevant when the data path is **not** an in-process memory lookup - i.e. **daemon mode** (`use_ldd=True`, where the SDK reads from a persistent store like Redis or DynamoDB instead of caching flags itself) or a **persistent feature store** for backup/cold-start. In default streaming mode, `variation()` is a hash-map lookup and this metric will be sub-millisecond and uninteresting.

The server-side measurement is implemented as a real `ldclient.Hook` (`_EvalLatencyHook` in [`ld_locust/server.py`](../locust/ld_locust/server.py)), so it captures every code path through `variation`/`variation_detail`/`migration_variation` uniformly, including any future hook stages added by the SDK.

The mobile-side measurement wraps `MobileLDClient.variation*` directly. Mobile flag evaluation is always an in-memory lookup against the pre-evaluated flag store, so this row will typically read 0&nbsp;ms.

---

## Reading the panel

A healthy Locust panel during a load test looks roughly like this (truncated):

```
type            name                                                         reqs   fails   avg_ms
sse:connect     http://ldrelay:8030/all                                         N        0       …
sse:connect     http://ldrelay:8030/meval/[context]                             N        0       …
GET             http://ldrelay:8030/msdk/evalx/contexts/[context]               N        0       …
POST            http://ldrelay:8030/bulk                                        N        0       …
POST            http://ldrelay:8030/diagnostic                                  N        0       …
POST            http://ldrelay:8030/mobile/events/bulk                          N        0       …
POST            http://ldrelay:8030/mobile/events/diagnostic                    N        0       …
PATCH           https://app.launchdarkly.com/api/v2/flags/<proj>/locust-heart   N        0       …
ld:init         server                                                          N        0      ~few ms
ld:init         mobile                                                          N        0      ~few ms
sse:flag-update server                                                          N        0      hundreds of ms
sse:flag-update mobile                                                          N        0      hundreds of ms
ld:eval         server.variation                                                N        0      ~0 ms in stream mode
ld:eval         mobile.variation                                                N        0      ~0 ms
```

Rows that don't begin with `ld:` or `sse:flag-update` are real network requests - see [`endpoints.md`](endpoints.md) for what each one means and how relay handles it.
