# Key metrics

## 1. Initialization Latency

This metric measures how long an SDK spends in its "not yet initialized" window. While the SDK is in this state, `variation()` calls return defaults rather than real flag values, so high initialization latency translates directly into user-visible incorrect flag evaluations on app cold start.

This table shows the rows the package emits:

| Row | What it measures |
|---|---|
| `ld:init server` | Time from server `LDClient` construction to first successful `put` from the streaming endpoint. |
| `ld:init mobile` | Time from `MobileLDClient` construction to the first `put` over `/meval`. |

The clock starts when the client object is created and stops the moment the SDK transitions to `is_initialized() == True`. For both server and mobile, this covers the time from client construction to the first `put` event arriving over the streaming connection.

In [data saving mode](../README.md#data-saving-mode-fdv2), a server user has two data sources that can produce that first payload: the `/sdk/poll` initializer and the `/sdk/stream` synchronizer. Whichever arrives first records the sample, and the row stays at one sample per client. With the `default` preset that is normally the initializer, which is the point of the mode - the initial payload comes off a cheap poll rather than a stream handshake.

## 2. Update Propagation Latency

This metric measures wall-clock time between a flag change at LaunchDarkly and the moment a connected SDK observes that change. **It is the right metric to track when measuring user impact of flag changes**, because it is the latency a real user experiences between "SDK admin saves a change" and "production traffic sees the new value."

This table shows the rows the package emits:

| Row | What it measures |
|---|---|
| `sse:flag-update server` | Wall-clock delta from when LaunchDarkly accepts a flag-variation PATCH to when the server SDK observes the resulting `patch` event over its streaming connection. In data saving mode, the equivalent `put-object` inside a change transfer. |
| `sse:flag-update mobile` | The same delta, measured on the mobile streaming endpoint `/meval`. |

The measurement loop runs as follows:

1. The Locust master spawns a background thread, `enable_flag_propagation_metric()` in [`ld_locust/heartbeat.py`](../locust/ld_locust/heartbeat.py). The thread PATCHes the `locust-heartbeat` flag every 30 seconds, writing the current epoch-millisecond clock into the variation values.
2. The instrumented SDKs receive the resulting `patch` event over their streaming connection, read the embedded timestamp, subtract it from the receive-time clock, and emit that delta as the metric.

This is end-to-end propagation, measured between the PATCH at LaunchDarkly's admin API and the SDK observing the change at the client.

The `locust-heartbeat` flag and its bootstrap policy are documented in the project [README](../README.md#optional-configure-a-flag-heartbeat). Without that flag and its API token, this metric row does not appear. The test still runs.

Only changes that arrive over a live connection count. A full data transfer - the initial payload, or a stream reconnect that starts from scratch - replays the heartbeat flag holding whatever timestamp it last had, which would otherwise register as a propagation delay of minutes. Those are skipped in both modes. In data saving mode the `polling` preset reports no `sse:flag-update` rows at all, since a poll-detected change carries the poll interval inside the measurement.

## 3. Evaluation Latency

This metric is mostly relevant when the data path is **not** an in-process memory lookup. Two configurations make it interesting:

- **Daemon mode**, set via `use_ldd=True`. The SDK reads from a persistent store such as Redis or DynamoDB instead of caching flags itself.
- **Persistent feature store** for backup and cold start.

In default streaming mode, `variation()` is a hash-map lookup. This metric reads sub-millisecond and is uninteresting.

This table shows the rows the package emits:

| Row | What it measures |
|---|---|
| `ld:eval server.variation` | Wall-clock duration of a single `LDClient.variation()` call. |
| `ld:eval server.variation_detail` | Wall-clock duration of a single `LDClient.variation_detail()` call. |
| `ld:eval mobile.variation` | The same, for the mobile emulator. |
| `ld:eval mobile.variation_detail` | The same, for the mobile emulator. |

The server-side measurement uses a real `ldclient.Hook`, named `_EvalLatencyHook` in [`ld_locust/server.py`](../locust/ld_locust/server.py). It captures every code path through `variation`, `variation_detail`, and `migration_variation` uniformly, including any future hook stages added by the SDK.

The mobile-side measurement wraps `MobileLDClient.variation*` directly. Mobile flag evaluation is always an in-memory lookup against the pre-evaluated flag store, so this row typically reads 0&nbsp;ms.

## Reading the panel

A healthy Locust panel during a load test looks roughly like the following truncated view:

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

To learn more about these requests, read [`endpoints.md`](endpoints.md).
