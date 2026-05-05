![screenshot](screenshot.png)


# LD-Relay Locust File


This is a Locust file that emulates the load of LaunchDarkly server and mobile clients against an [LD Relay Proxy](https://github.com/launchdarkly/ld-relay) instance.


# Setup

## Prerequisites

To run the testing infrastructure outside of Docker, you need Python 3.10+, [Locust 2.x](https://docs.locust.io/), and the LaunchDarkly server SDK 9.x.

To install dependencies from the project root:

```sh
pip install --upgrade pip
pip install -r locust/requirements.txt
pip install 'locust>=2.43,<3'
```


## Optional: Configure a flag heartbeat

A flag heartbeat lets you gather metrics on how long a flag update takes to propagate to clients. If you skip these steps, the tests still run, but the `sse:flag-update` metric does not appear.

To configure a flag heartbeat:

1. Create the flag. To learn more, read [Creating the heartbeat flag](#creating-the-heartbeat-flag).
2. Create an API token that can modify the flag. To learn more, read [Locust API token policy](#locust-api-token-policy).

### Creating the heartbeat flag

The easiest path uses [`ldcli`](https://launchdarkly.com/docs/home/getting-started/ldcli).

Here is an example:

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

The `clientSideAvailability` block makes mobile and JavaScript clients see the flag. Without it, the mobile-side `sse:flag-update` metric never populates.

The token used to run that command needs `createFlag` on `proj/<your_project>:env/*:flag/locust-heartbeat`. You can also create the flag from the LaunchDarkly UI manually. The token policy in [Locust API token policy](#locust-api-token-policy) below already grants this permission, so you can use the same service token for both bootstrapping the flag and running the heartbeat thread.


## Modify locustfile.py

The `ld_locust` package alongside `locustfile.py` defines the two `User` base classes you subclass:

- `LaunchDarklyLocust`, in [`locust/ld_locust/server.py`](locust/ld_locust/server.py).
- `LaunchDarklyMobileLocust`, in [`locust/ld_locust/mobile.py`](locust/ld_locust/mobile.py).

> **Note:** Data Saving Mode is not yet supported.

The `locustfile.py` file contains two `SequentialTaskSet` classes and two `User` subclasses. The example tasks initialize a client, evaluate random flags, and occasionally call `track` and `identify`. Edit them to match your workload.

To pass extra `Config` arguments to the underlying server `LDClient`, set `config_overrides` on your subclass.

Here is an example:

```python
class MyServerLoad(LaunchDarklyLocust):
    tasks = [LaunchDarklyServerTaskSet]
    config_overrides = {"evaluation_reasons": True}
```

For mobile users, override the class attributes that `LaunchDarklyMobileLocust` exposes, such as `send_events`, `sdk_key`, and `stream_uri`. For finer-grained changes, override `make_client()` to construct your own `MobileLDClient`.

Locust uses `weight` on each `User` subclass to control the population mix. To learn more, read the [Locust docs](https://docs.locust.io/en/stable/writing-a-locustfile.html).


## Set your environment variables

The required environment variables are:

```
LAUNCHDARKLY_SDK_KEY=sdk-xxxx
LAUNCHDARKLY_MOBILE_KEY=mob-xxxx

# Optional: enables flag-update propagation timings
LAUNCHDARKLY_HEARTBEAT_PROJECT=my-proj-key
LAUNCHDARKLY_HEARTBEAT_API_KEY=api-xxxx

# Optional: LaunchDarkly admin API base URL. Defaults to
# https://app.launchdarkly.com. Set to https://app.launchdarkly.us
# for federal, or to your EU region URL.
LAUNCHDARKLY_API_BASE=https://app.launchdarkly.com
```

The relay's hostname comes from Locust's host setting, which you can supply as `--host`, `LOCUST_HOST`, or via the Locust UI when you start the swarm. Every endpoint the SDKs hit defaults to that host.

To override individual endpoints (for example, when relay, events, and stream live on different hosts), set any of:

```
LAUNCHDARKLY_BASE_URI
LAUNCHDARKLY_EVENTS_URI
LAUNCHDARKLY_STREAM_URI
LAUNCHDARKLY_MOBILE_STREAM_URI
```

You can also set the `sdk_key` attribute on each subclass instead of using the environment variable.

Here is an example:

```python
class MyLocust(LaunchDarklyMobileLocust):
    sdk_key = "mob-xxx"
    # or use a different environment variable
    sdk_key = os.environ.get('SOME_OTHER_ENV')
```


## Running the tests

To run the tests:

1. Run Locust from the project root:

   ```sh
   python -m locust -f locust/locustfile.py
   ```

2. Open `http://localhost:8089`. Enter your desired user count and your LD-Relay instance as the host.

## Running in Docker

To build and run the image, replace `ldrelay` with your relay's hostname:

```sh
docker build -t locust-ld-relay .
docker run -p 8089:8089/tcp -e LOCUST_HOST=http://ldrelay:8030 --env-file=env locust-ld-relay:latest
```

## Locally testing with Docker Compose

A `docker-compose.yml` is provided for local testing. Set `LAUNCHDARKLY_SDK_KEY` and `LAUNCHDARKLY_MOBILE_KEY` in your env, optionally set the heartbeat variables, then run:

```sh
docker compose up --build
```

Connect to `http://localhost:8089`.

## Running in distributed mode

To run distributed, configure one master and one or more workers.

The master uses this environment variable:

```
LOCUST_MODE_MASTER=true
```

Expose port 5557 from the master to your workers.

Each worker uses these environment variables:

```
LOCUST_MODE_WORKER=true
LOCUST_MASTER_NODE_HOST=<hostname of master>
```

To learn more, read the [Locust distributed-mode docs](https://docs.locust.io/en/stable/running-distributed.html).


## FAQ

### Why RPS values appear lower than expected

In Locust, users are concurrent virtual clients executing the task tree. They are not requests. Each LaunchDarkly client opens a long-lived streaming connection to the Flag Delivery Network and periodically POSTs analytics events. Server-side streaming clients will only establish a single streaming connection over it's lifetime. Evaluating a flag does not cause a HTTP request directly.


## Development

Dev tooling lives in [`mise.toml`](mise.toml): `pre-commit`, `ruff`, and `ldcli`.

With [mise](https://mise.jdx.dev/) installed, run the one-time setup:

```sh
mise install
pre-commit install
```

[Pre-commit](https://pre-commit.com/) runs [ruff](https://docs.astral.sh/ruff/) and [shellcheck](https://www.shellcheck.net/) on every `git commit`. To run them manually across the whole tree:

```sh
pre-commit run --all-files
```

To skip mise, run `pip install pre-commit` followed by `pre-commit install`. The same hooks run in CI on pushes and PRs, defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml), alongside a `docker build` smoke test.


# Key metrics

When evaluating the performance of your LD Relay deployment from an application perspective, there are a few key metrics to consider:


| Metric | Stat row(s) | What it measures |
|---|---|---|
| **Initialization Latency** | `ld:init server`, `ld:init mobile` | Time from SDK construction to receiving the initial payload from the flag delivery network. After this point, fallback values will not be served |
| **Update Propagation Latency** | `sse:flag-update server`, `sse:flag-update mobile` | Time between a flag update being made and the SDK receiving it. This is estimated by creating a heartbeat flag and storing a timestamp in it's variation. To learn more, read [Configure a flag heartbeat](#optional-configure-a-flag-heartbeat). |
| **Evaluation Latency** | `ld:eval server.variation`, `ld:eval mobile.variation`, plus `*.variation_detail` | Time spent inside `variation()` and `variation_detail()`. Most meaningful in daemon mode or with a persistent feature store, where evaluation is no longer a pure in-memory lookup. |

The underlying SSE and HTTP traffic also shows up under standard rows: `sse:connect`, `GET`, and `POST` to `/bulk`, `/mobile/events/bulk`, `/mobile/events/diagnostic`, and similar paths.

For a fuller treatment of what each metric measures, how to read the panel, and what each endpoint row means, read the following:

- **[`docs/metrics.md`](docs/metrics.md)** describes the four key metrics and how to read them.
- **[`docs/endpoints.md`](docs/endpoints.md)** describes the SDK endpoints that show up as rows in Locust. Pair this with [relay's own endpoint reference](https://github.com/launchdarkly/ld-relay/blob/v8/docs/endpoints.md).

# Additional metrics

To get additional metrics from LD-Relay, use its built-in Prometheus support along with the Prometheus node exporter. For LD Relay v9+, use the built-in [OpenTelemetry export](https://github.com/launchdarkly/ld-relay/blob/v9/docs/metrics.md).

You can also export Locust stats through its [OpenTelemetry integration](https://docs.locust.io/en/stable/telemetry.html).


# Locust API token policy

The flag-propagation metric `sse:flag-update` is driven by the master process. The master PATCHes the `locust-heartbeat` flag every 30 seconds. The token attached to that PATCH, set in `LAUNCHDARKLY_HEARTBEAT_API_KEY`, needs the policy below.

The policy allows the master to:

- Create the flag if it does not yet exist.
- Update its variation values.
- Bypass any required-approval workflow on those updates.
- Read project metadata. LaunchDarkly now requires `viewProject` for any project-scoped action.

Here is an example policy:

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

You can attach the policy directly to a service token. A custom role is not required. The heartbeat thread just needs a token with that policy. Both paths below assume you have already run `ldcli login` with an account-admin token.

### Option A: inline policy on a service token

This is the simplest path.

Here is an example:

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

The printed string is the bearer token. Copy it into `LAUNCHDARKLY_HEARTBEAT_API_KEY`. LaunchDarkly only reveals the value once. To rotate it, run `ldcli access-tokens reset`.

### Option B: reusable custom role and service token

Use Option B if you want multiple tokens, for example across teams, to share the same permissions, or if you want the policy editable in the LaunchDarkly UI.

Here is an example:

```sh
# 1. Create the custom role and capture its _id. customRoleIds takes the
#    opaque _id, not the human-readable key.
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

> **Note:** Custom roles are an Enterprise feature. If your account does not have them, use Option A.

The heartbeat thread picks up the token from `LAUNCHDARKLY_HEARTBEAT_API_KEY` on its next 30-second tick.
