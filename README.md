![screenshot](screenshot.png)


# LD-Relay Locust File


This is a locust file meant to emulate the load of LaunchDarkly server and mobile clients against an [LD Relay Proxy](https://github.com/launchdarkly/ld-relay) instance.


# Setup

## Prerequisites

If you are running the testing infrastructure outside of Docker you will need Python 3.10+ along with [Locust 2.x](https://docs.locust.io/) and the LaunchDarkly server SDK 9.x. From the project root:

```sh
pip install --upgrade pip
pip install -r locust/requirements.txt
pip install 'locust>=2.43,<3'
```

(The Docker image bundles a pinned `locustio/locust` base, so you do not need to install Locust separately when using Docker.)


## Optional: Configure a flag heartbeat

This will allow you to gather metrics on how long it takes for a flag update to propagate to clients. If you do not follow these steps, the tests will still run, but you will not get the `sse:flag-update` metric.

1. Create the flag (a `number` flag with two variations) - see [Creating the heartbeat flag](#creating-the-heartbeat-flag).
2. Create an API key that can modify it - see [Locust API token policy](#locust-api-token-policy).

### Creating the heartbeat flag

Easiest path is via [`ldcli`](https://launchdarkly.com/docs/home/getting-started/ldcli):

```sh
ldcli flags create --project <YOUR_PROJECT_KEY> -ojson --data "$(jq -n '{
  key: "locust-heartbeat",
  name: "Locust Heartbeat",
  description: "Updated by ld-relay-locustfile master to time flag propagation. Variation values are current epoch-ms.",
  kind: "multivariate",
  temporary: false,
  tags: ["locust", "load-test"],
  variations: [
    {value: 0, name: "tick-a"},
    {value: 1, name: "tick-b"}
  ],
  defaults: {onVariation: 0, offVariation: 0},
  clientSideAvailability: {usingMobileKey: true, usingEnvironmentId: true}
}')"
```

The `clientSideAvailability` block is what makes mobile (and JS) clients see the flag - without it the mobile-side `sse:flag-update` metric will never populate.

The token used to run that command (or run it from the LaunchDarkly UI manually) needs `createFlag` on `proj/<your_project>:env/*:flag/locust-heartbeat`. The token policy in [Locust API token policy](#locust-api-token-policy) below already grants this, so you can use the same service token for both bootstrapping the flag and running the heartbeat thread.


## Modify locustfile.py

Take a look at `launchdarkly_locust.py`. It defines `LaunchDarklyLocust` and `LaunchDarklyMobileLocust`, the two `User` base classes that you will subclass.

Open `locustfile.py`. You'll see two `SequentialTaskSet` classes and two `User` subclasses. Edit them to emulate the behavior of your real users. The example task sets initialize a client and then evaluate random flags, occasionally calling `track`/`identify`.

Pass extra `Config` arguments to the underlying `LDClient` (server) by setting `config_overrides` on your subclass:

```python
class MyServerLoad(LaunchDarklyLocust):
    tasks = [LaunchDarklyServerTaskSet]
    config_overrides = {"evaluation_reasons": True}
```

For mobile users override individual class attributes (`evaluation_reasons`, `send_events`, etc.) - the mobile emulator does not use the server `Config` object.

Locust uses `weight` on each `User` subclass to control the population mix. See the [Locust docs](https://docs.locust.io/en/stable/writing-a-locustfile.html) for more.


## Setup your environment variables

```
LAUNCHDARKLY_SDK_KEY=sdk-xxxx
LAUNCHDARKLY_MOBILE_KEY=mob-xxxx
LAUNCHDARKLY_BASE_URI=http://ldrelay:8030
LAUNCHDARKLY_EVENTS_URI=http://ldrelay:8030
LAUNCHDARKLY_STREAM_URI=http://ldrelay:8030
LAUNCHDARKLY_MOBILE_STREAM_URI=http://ldrelay:8030

# Optional - enables flag-update propagation timings
LAUNCHDARKLY_HEARTBEAT_PROJECT=my-proj-key
LAUNCHDARKLY_HEARTBEAT_API_KEY=api-xxxx
```

You can also set the `sdk_key` attribute on each subclass instead of the env var:

```python
class MyLocust(LaunchDarklyMobileLocust):
    sdk_key = "mob-xxx"
    # or use a different environment variable
    sdk_key = os.environ.get('SOME_OTHER_ENV')
```


## Running the tests

- Run locust with `python -m locust -f locust/locustfile.py`
- Open `http://localhost:8089` and enter your desired user count and host (your LD-Relay instance).

## Running in Docker

```sh
docker build -t locust-ld-relay .
docker run -p 8089:8089/tcp -e TARGET_URL=http://ldrelay:8030 --env-file=env locust-ld-relay:latest
```

### Connecting to a remote machine from the Docker container
Use `--add-host` to update the container's `/etc/hosts`:

```sh
docker run -p 8089:8089/tcp \
  --add-host="ldrelay:100.x.x.x" \
  -e TARGET_URL=http://ldrelay:8030 \
  --env-file=env \
  locust-ld-relay:latest
```


### Connecting to the host machine from the Docker container
> **NOTE:** Requires Docker >= 20.10

Configure your env file to use `host.docker.internal`:

```
LAUNCHDARKLY_BASE_URI=http://host.docker.internal:8030
LAUNCHDARKLY_EVENTS_URI=http://host.docker.internal:8030
LAUNCHDARKLY_STREAM_URI=http://host.docker.internal:8030
LAUNCHDARKLY_MOBILE_STREAM_URI=http://host.docker.internal:8030
```

Then:

```sh
docker run -p 8089:8089/tcp \
  --add-host=host.docker.internal:host-gateway \
  -e TARGET_URL=http://host.docker.internal:8030 \
  --env-file=env \
  locust-ld-relay:latest
```

## Locally testing with Docker compose

A simple `docker-compose.yml` is provided for local testing. Set `LAUNCHDARKLY_SDK_KEY` and `LAUNCHDARKLY_MOBILE_KEY` (and optionally the heartbeat vars), then:

```sh
docker compose up --build
```

and connect to `http://localhost:8089`.

## Running in distributed mode

To run distributed:

### Master
```
LOCUST_MODE_MASTER=true
```
Expose port 5557 from the master to your workers.

### Workers
```
LOCUST_MODE_WORKER=true
LOCUST_MASTER_NODE_HOST=<hostname of master>
```

See the [Locust distributed-mode docs](https://docs.locust.io/en/stable/running-distributed.html) for details.


## FAQ

### Why is the RPS so low?

Users in Locust are not requests; they are concurrent virtual clients executing the task tree. Each LD client makes a streaming connection plus periodic eval/track requests, so a few hundred users may produce only a handful of REST requests per second but many thousands of in-flight SSE connections.


## Development

Dev tooling is pinned in [`mise.toml`](mise.toml) (`pre-commit`, `ruff`, `ldcli`). With [mise](https://mise.jdx.dev/) installed, one-time setup is:

```sh
mise install                    # bootstrap pre-commit, ruff, ldcli
pre-commit install              # enable the git hook
```

Lints + formatting are wired up via [pre-commit](https://pre-commit.com/) with [ruff](https://docs.astral.sh/ruff/) and [shellcheck](https://www.shellcheck.net/) - they run automatically on every `git commit`. To run them manually across the whole tree:

```sh
pre-commit run --all-files
```

If you'd rather not use mise, `pip install pre-commit` followed by `pre-commit install` works the same. The same hooks run in CI on pushes and PRs (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)), alongside a `docker build` smoke test.


# LaunchDarkly metrics

The package emits three LaunchDarkly-specific metrics into the standard Locust stats panel. Each appears once per side (server / mobile) so you can compare server SDK behavior with the mobile-emulator behavior under the same load:

| Metric | Stat row(s) | What it measures |
|---|---|---|
| **Initialization Latency** | `ld:init server` / `ld:init mobile` | Time from SDK construction until the first successful `put`/`ping` reply leaves the client in an initialized state. |
| **Update Propagation Latency** | `sse:flag-update server` / `sse:flag-update mobile` | Round-trip time from the heartbeat PATCH at LaunchDarkly through Relay to the client's SSE consumer. Driven by the `locust-heartbeat` flag (see [Configure a flag heartbeat](#optional-configure-a-flag-heartbeat)). |
| **Evaluation Latency** | `ld:eval server.variation` / `ld:eval mobile.variation` (and `*.variation_detail`) | Time spent inside `variation()` / `variation_detail()`. Most meaningful in daemon mode or with a persistent feature store, where evaluation is no longer a pure in-memory lookup. |

In addition to those, the underlying SSE/HTTP traffic shows up under its standard rows (`sse:connect`, `GET`, `POST` to `/bulk`, `/mobile/events/{bulk,diagnostic}`, etc.).

For a fuller treatment - what each metric is actually measuring, how to read the panel, and what each endpoint row means - see:

- **[`docs/metrics.md`](docs/metrics.md)** - the four key metrics (throughput, init, propagation, evaluation) and how to interpret them
- **[`docs/endpoints.md`](docs/endpoints.md)** - the SDK endpoints that show up as rows in Locust, paired with [relay's own endpoint reference](https://github.com/launchdarkly/ld-relay/blob/v8/docs/endpoints.md)

# Additional metrics

You can get additional metrics from LD-Relay using its built-in Prometheus support along with the Prometheus node exporter. For LD Relay v9+, you can use the built-in [OpenTelemetry export](https://github.com/launchdarkly/ld-relay/blob/v9/docs/metrics.md).

You can also export Locust stats via its [OpenTelemetry integration](https://docs.locust.io/en/stable/telemetry.html).


# Locust API token policy

The flag-propagation metric (`sse:flag-update`) is driven by the master process PATCHing the `locust-heartbeat` flag every 30 seconds. The token attached to that PATCH (`LAUNCHDARKLY_HEARTBEAT_API_KEY`) needs the policy below. It allows the master to (a) create the flag if it doesn't yet exist, (b) update its variation values, (c) bypass any required-approval workflow on those updates, and (d) read project metadata (LaunchDarkly now requires `viewProject` to do anything project-scoped).

```json
[
  {
    "resources": [
      "proj/*:env/*:flag/locust-heartbeat"
    ],
    "actions": [
      "createFlag",
      "updateFlagVariations",
      "bypassRequiredApproval"
    ],
    "effect": "allow"
  },
  {
    "resources": [
      "proj/*"
    ],
    "actions": [
      "viewProject"
    ],
    "effect": "allow"
  }
]
```

## Provisioning the role and token with `ldcli`

You can attach the policy directly to a service token (no custom role required) — the heartbeat thread just needs *a* token with that policy. The two paths below assume you've already run `ldcli login` with an account-admin token.

### Option A: inline policy on a service token (simplest)

```sh
POLICY=$(cat <<'EOF'
[
  {
    "resources": ["proj/*:env/*:flag/locust-heartbeat"],
    "actions": ["createFlag", "updateFlagVariations", "bypassRequiredApproval"],
    "effect": "allow"
  },
  {
    "resources": ["proj/*"],
    "actions": ["viewProject"],
    "effect": "allow"
  }
]
EOF
)

ldcli access-tokens create -ojson --data "$(jq -n --argjson policy "$POLICY" '{
  name: "locust-heartbeat",
  serviceToken: true,
  inlineRole: $policy
}')" | jq -r '.token'
```

The printed string is the bearer token — copy it into `LAUNCHDARKLY_HEARTBEAT_API_KEY`. (LaunchDarkly only reveals the value once; rotate via `ldcli access-tokens reset`.)

### Option B: reusable custom role + service token

Use this if you want multiple tokens (e.g. across teams) to share the same permissions, or you want the policy editable in the LaunchDarkly UI.

```sh
# 1. Create the custom role and capture its _id (customRoleIds takes the
#    opaque _id, not the human-readable key).
ROLE_ID=$(ldcli custom-roles create -ojson --data "$(jq -n '{
  key: "locust-heartbeat",
  name: "Locust Heartbeat",
  description: "Allows the locust load tester to drive the locust-heartbeat flag.",
  policy: [
    {
      resources: ["proj/*:env/*:flag/locust-heartbeat"],
      actions:   ["createFlag", "updateFlagVariations", "bypassRequiredApproval"],
      effect:    "allow"
    },
    {
      resources: ["proj/*"],
      actions:   ["viewProject"],
      effect:    "allow"
    }
  ]
}')" | jq -r '._id')

# 2. Issue a service token bound to that role
ldcli access-tokens create -ojson --data "$(jq -n --arg id "$ROLE_ID" '{
  name: "locust-heartbeat",
  serviceToken: true,
  customRoleIds: [$id]
}')" | jq -r '.token'
```

> **Note:** custom roles are an Enterprise feature. If your account doesn't have them, use Option A.

The heartbeat thread will pick the token up from `LAUNCHDARKLY_HEARTBEAT_API_KEY` on its next 30-second tick.
