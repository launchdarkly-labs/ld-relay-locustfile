"""
Default implementation of feature flag polling requests.
"""
# currently excluded from documentation - see docs/README.md

from collections import namedtuple
import json
import urllib3

from ldclient.interfaces import FeatureRequester
from ldclient.util import UnsuccessfulResponseException
from util import _headers, create_http_pool_manager
from ldclient.util import log
from ldclient.util import throw_if_unsuccessful_response
from ldclient.versioned_data_kind import FEATURES, SEGMENTS
from ldclient.user_filter import UserFilter
from base64 import urlsafe_b64encode

EVALX_GET = '/msdk/evalx/users'
EVALX_REPORT = '/msdk/evalx/user'

CacheEntry = namedtuple('CacheEntry', ['data', 'etag'])

class FeatureRequesterImpl(FeatureRequester):
    def __init__(self, environment, config):
        self._cache = dict()
        self._http = create_http_pool_manager(environment, num_pools=1, verify_ssl=config.verify_ssl,
            target_base_uri=config.base_uri, force_proxy=config.http_proxy)
        self._config = config

    def get_all_data(self):
        all_data = self._do_request(self._config.base_uri, True)
        for k,v in all_data.items():
          v['key'] = k
        return {
            FEATURES: all_data
        }

    def get_one(self, kind, key):
        raise ValueError('mobile feature requester does not support this')

    def _do_request(self, base_uri, allow_cache):
        hdrs = _headers(self._config.sdk_key, client='PythonMobile')
        method = "GET"
        body = None
        uri = base_uri
        cache_uri = uri + EVALX_GET + '/' + self._config.user_b64
        if self._config.use_report:
          method = 'REPORT'
          body = self._config.user_json
          hdrs.update({'Content-Type': 'application/json'})
          uri = uri + EVALX_REPORT
        else:
          uri = cache_uri
        if self._config.evaluation_reasons:
          uri += '?withReasons=true'
          cache_uri += '?withReasons=true'

        if allow_cache:
            cache_entry = self._cache.get(cache_uri)
            if cache_entry is not None:
                hdrs['If-None-Match'] = cache_entry.etag

        r = self._http.request(method, uri,
                               headers=hdrs,
                               timeout=urllib3.Timeout(connect=self._config.connect_timeout, read=self._config.read_timeout),
                               retries=1,
                               body=body)
        throw_if_unsuccessful_response(r)
        if r.status == 304 and allow_cache and cache_entry is not None:
            data = cache_entry.data
            etag = cache_entry.etag
            from_cache = True
        else:
            data = json.loads(r.data.decode('UTF-8'))
            etag = r.getheader('ETag')
            from_cache = False
            if allow_cache and etag is not None:
                self._cache[uri] = CacheEntry(data=data, etag=etag)
        log.debug("%s response status:[%d] From cache? [%s] ETag:[%s]",
            uri, r.status, from_cache, etag)
        return data
