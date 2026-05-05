#!/bin/bash

curl -H "Authorization: $LAUNCHDARKLY_MOBILE_KEY" -H "X-LaunchDarkly-Event-Schema: 3" -H "Content-Type: application/json"  -H "User-Agent: PythonClient/6.11.3" -H "Accept: application/json" -d @example-event.json "$@"
