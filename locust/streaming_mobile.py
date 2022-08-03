"""
Default implementation of the streaming component.
"""
# currently excluded from documentation - see docs/README.md

from collections import namedtuple

import json
from threading import Thread

import backoff
import logging
import time

from ldclient.interfaces import UpdateProcessor
from sse_client import SSEClient
from ldclient.util import _stream_headers, log, UnsuccessfulResponseException, http_error_message, is_http_error_recoverable
from ldclient.versioned_data_kind import FEATURES, SEGMENTS
from locust.event import Events

# allows for up to 5 minutes to elapse without any data sent across the stream. The heartbeats sent as comments on the
# stream will keep this from triggering
stream_read_timeout = 5 * 60

STREAM_PATH= '/meval'

ParsedPath = namedtuple('ParsedPath', ['kind', 'key'])

class MobileStreamingUpdateProcessor(Thread, UpdateProcessor):
    def __init__(self, environment, config, requester, store, ready):
        Thread.__init__(self)
        self.daemon = True
        self._uri = config.stream_base_uri + STREAM_PATH + '/' + config.user_b64
        self._environment = environment
        self._config = config
        self._requester = requester
        self._store = store
        self._running = False
        self._ready = ready

        # We need to suppress the default logging behavior of the backoff package, because
        # it logs messages at ERROR level with variable content (the delay time) which will
        # prevent monitors from coalescing multiple messages. The backoff package attempts
        # to suppress its own output by default by giving the logger a NullHandler, but it
        # will still propagate up to the root logger unless we do this:
        logging.getLogger('backoff').propagate = False

    # Retry/backoff logic:
    # Upon any error establishing the stream connection we retry with backoff + jitter.
    # Upon any error processing the results of the stream we reconnect after one second.
    def run(self):
        log.info("Starting MobileStreamingUpdateProcessor connecting to uri: " + self._uri)
        self._running = True
        while self._running:
            try:
                init_start = time.time()
                messages = self._connect()
                for msg in messages:
                    if not self._running:
                        break
                    message_ok = self.process_message(self._environment, self._store, self._requester, msg)
                    if message_ok is True and self._ready.is_set() is False:
                        log.info("MobileStreamingUpdateProcessor initialized ok.")
                        init_duration = int((time.time() - init_start) * 1000)
                        self._environment.events.request.fire(request_type="ld:init", name="mobile", response_time=init_duration, response_length=0, exception=None, context=None)
                        self._ready.set()
            except UnsuccessfulResponseException as e:
                log.error(http_error_message(e.status, "stream connection"))
                init_duration = int((time.time() - init_start) * 1000)
                self._environment.events.request.fire(request_type="ld:init", name="mobile", response_time=init_duration, response_length=0, exception=e, context=None)
                if not is_http_error_recoverable(e.status):
                    self._ready.set()  # if client is initializing, make it stop waiting; has no effect if already inited
                    self.stop()
                    break
            except Exception as e:
                init_duration = int((time.time() - init_start) * 1000)
                self._environment.events.request.fire(request_type="ld:init", name="mobile", response_time=init_duration, response_length=0, exception=e, context=None)
                log.warning("Caught exception. Restarting stream connection after one second. %s" % e)
                # no stacktrace here because, for a typical connection error, it'll just be a lengthy tour of urllib3 internals
            time.sleep(1)

    def _backoff_expo():
        return backoff.expo(max_value=30)

    def should_not_retry(e):
        return isinstance(e, UnsuccessfulResponseException) and (not is_http_error_recoverable(e.status))

    def log_backoff_message(props):
        log.error("Streaming connection failed, will attempt to restart")
        log.info("Will reconnect after delay of %fs", props['wait'])

    @backoff.on_exception(_backoff_expo, BaseException, max_tries=None, jitter=backoff.full_jitter,
                          on_backoff=log_backoff_message, giveup=should_not_retry)
    def _connect(self):
        return SSEClient(
            self._uri,
            headers=_stream_headers(self._config.sdk_key),
            connect_timeout=self._config.connect_timeout,
            read_timeout=stream_read_timeout,
            verify_ssl=self._config.verify_ssl,
            http_proxy=self._config.http_proxy)

    def stop(self):
        log.info("Stopping MobileStreamingUpdateProcessor")
        self._running = False

    def initialized(self):
        return self._running and self._ready.is_set() is True and self._store.initialized is True

    # Returns True if we initialized the feature store
    @staticmethod
    def process_message(environment, store, requester, msg):
        if msg.event == 'put':
            all_data = json.loads(msg.data)

            for k,v in all_data.items():
                v['key'] = k

            init_data = {
                FEATURES: all_data
            }
            log.debug("Received put event with %d flags",
                len(init_data[FEATURES]))
            store.init(init_data)
            return True
        elif msg.event == 'patch':
            recv_time = time.time() * 1000
            payload = json.loads(msg.data)
            if payload.get('key') == 'locust-heartbeat':
                value = int(payload.get('value') or 0)
                duration = int((recv_time - value))
                environment.events.request.fire(request_type='sse:flag-update', name='/meval', response_time=duration, response_length=0, exception=None, context=None)

            log.debug("Received patch event for %s, New version: [%d]", payload.get('key'), payload.get("version"))
            store.upsert(FEATURES, payload)
        elif msg.event == 'ping':
            log.debug('Received ping event')
            all_data = requester.get_all_data()
            store.init(all_data)
            log.debug("Received flags after ping event with %d flags", len(all_data[FEATURES]))
            return True
            
        elif msg.event == 'delete':
            payload = json.loads(msg.data)
            key = payload.get('key')
            # noinspection PyShadowingNames
            version = payload['version']
            log.debug("Received delete event for %s, New version: [%d]", key, version)
            target = ParsedPath(kind = FEATURES, key = key)
            store.delete(target.kind, target.key, version)
        else:
            log.warning('Unhandled event in stream processor: ' + msg.event)
        return False

    @staticmethod
    def _parse_path(path):
        raise ValueError('not supposed to use this in mobile client stream')
        for kind in [FEATURES, SEGMENTS]:
            if path.startswith(kind.stream_api_path):
                return ParsedPath(kind = kind, key = path[len(kind.stream_api_path):])
        return None

    # magic methods for "with" statement (used in testing)
    def __enter__(self):
        return self
    
    def __exit__(self, type, value, traceback):
        self.stop()
