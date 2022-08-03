import os
import sys
import logging
import gevent
import random
import gevent
import json
import time

from locust import TaskSet, SequentialTaskSet, task, between
# from locust.events import quitting
# import locust.runners as runners

from locust import HttpLocust, TaskSet, TaskSequence, task, between, seq_task, Locust
from locust.events import quitting
import locust.runners as runners

from gevent.event import Event

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from util import create_http_pool_manager
from launchdarkly_locust import LaunchDarklyLocust, LaunchDarklyMobileLocust

log = logging.getLogger()
log.setLevel(logging.DEBUG)

# example task set for launchdarkly mobile clients
class LaunchDarklyMobileTaskSet(SequentialTaskSet):
    # wait_time = between(1, 30)

    # first we will initialize the client
    # it will be automatically initialized by accessing the ldclient property
    # see the LaunchDarklyClass in launchdarkly.py for details

    @task
    def init_ldclient(self):
        log.debug('connecting to client')
        if not self.parent.ldclient.is_initialized():
            self.parent.close_client()
            raise Exception('failed to initialize client')

    @task
    class LDMobileTasks(TaskSet):
        # we are now initialized so let's do some tasks
        # we will execute one of the following tasks every 1-30s
        wait_time = between(1, 30)

        @task(10)
        def evaluate_flags(self):
            # evaluate a random flag
            # replace this with self.parent.ldclient.variation(flag, default) calls
            # that match a realistic workload
            flags = self._parent.parent.ldclient.all_flags_state().to_values_map()
            flag, value = random.choice(list(flags.items()))
            log.debug('evaluating flag %s', flag)
            self._parent.parent.ldclient.variation(flag, None)

        @task(8)
        def reindentify(self):
            ##  re-identify with new properties
            self._parent.parent.ldclient.identify({
                "key": "anonymous",
                "anonymous": True,
                "custom": {
                    "groups": ["admin"]
                }
            })
            if not self._parent.parent.ldclient.is_initialized():
                log.debug('failed to initialize after re-identify')
                self.locust.close_client()

        @task(3)
        def track_event(self):
            # send a tracked event
            self._parent.parent.ldclient.track('test_event', metric_value=1)

        @task(1)
        def done(self):
            # stop executing the LDMobileTasks task set
            self.interrupt()

    @task
    def disconnect(self):
        # when the LDMobileTask set is done executing, close the client
        # the locust will loop back to the first task and init a new client
        # this simulates a user leaving your application or losing network connectivity
        self.parent.close_client()

    def on_stop(self):
        self.parent.close_client()

class LaunchDarklyServerTaskSet(SequentialTaskSet):
    @task
    def init_ldclient(self):
        log.debug('connecting to client')
        ldclient = self.parent.ldclient
        if not ldclient.is_initialized():
            self.parent.close_client()
            # self.user.close_client()
            raise Exception('failed to initialize client')

    @task
    class LDServerTasks(TaskSet):
        # wait_time = between(1, 30)

        @task(10)
        def evaluate_flags(self):
            flags = self._parent.parent.ldclient.all_flags_state(self._parent.parent.user).to_values_map()
            flag, value = random.choice(list(flags.items()))
            log.debug('evaluating flag %s', flag)
            self._parent.parent.ldclient.variation(flag,self._parent.parent.user, None)

        @task(3)
        def track_event(self):
            self._parent.parent.ldclient.track('test_event',self._parent.parent.user, metric_value=1)

        @task(1)
        def done(self):
            self.interrupt()

    @task
    def disconnect(self):
        self.parent.close_client()

    def on_stop(self):
        self.parent.close_client()

class LaunchDarklyBasicServer(LaunchDarklyLocust):
    tasks = [LaunchDarklyServerTaskSet]
    weight = 1

class LaunchDarklyBasicMobile(LaunchDarklyMobileLocust):
    tasks = [LaunchDarklyMobileTaskSet]
    weight = 2
    # you can define any properties normally passed to LDClient.Config here
    # for example
    # evaluation_reasons=True

# optionally try and load env from a json file
# makes it easier to provision with terraform/cloudinit
try:
    with open(os.environ.get('LD_ENV_FILE', '/etc/ldlocust.json')) as json_file:
        log.debug('loading env from json file')
        data = json.load(json_file)
        for k, v in data.items():
            if os.environ.get(k) is None:
                log.debug('setting %s to %s', k, v)
                os.environ[k] = str(v)
except Exception:
    pass

def update_heartbeat_thread():
    proj = os.environ.get('LOCUST_HEARTBEAT_PROJECT')
    api_key = os.environ.get('LOCUST_HEARTBEAT_API_KEY')
    if not proj:
        log.error('set LOCUST_HEARTBEAT_PROJECT in order to enable flag update metrics')
        return
    if not api_key:
        log.error('set LOCUST_HEARTBEAT_API_KEY in order to enable flag update metrics')
        return
   
    flag = 'locust-heartbeat'
    url_template = 'https://app.launchdarkly.com/api/v2/flags/{}/{}'
    update_endpoint = url_template.format(proj, flag)
    # wait for the runner
    while runners.locust_runner is None:
        gevent.sleep(5)
    
    if is_master():
        pool = create_http_pool_manager(num_pools=1, verify_ssl=True, target_base_uri='https://api.launchdarkly.com')

        while True:
            now = int(time.time() * 1000)
            body = [{
                "op": "replace",
                "path": "/variations/0/value",
                "value": now
            }, {
                "op": "replace",
                "path": "/variations/1/value",
                "value": now+1
            }]
            try:
                resp = pool.request('PATCH', update_endpoint, headers={
                    'Authorization': api_key,
                    'Content-Type': 'application/json-patch+json',
                    'Accept': 'application/json'
                }, body=json.dumps(body).encode('utf-8'))
            except Exception as e:
                log.error('failed to update heartbeat flag %s', e)
            gevent.sleep(30)



def is_master():
    return isinstance(runners.locust_runner, runners.MasterLocustRunner) or isinstance(runners.locust_runner, runners.LocalLocustRunner)

thread = gevent.spawn(update_heartbeat_thread)
quitting += lambda: gevent.kill(thread)




#x = MobileConfig(sdk_key=os.environ.get('LAUNCHDARKLY_MOBILE_KEY'))
#c = MobileLDClient({"key": "anonymous", "anonymous": True}, config=x)
#
#c.variation("fooooo", None)
