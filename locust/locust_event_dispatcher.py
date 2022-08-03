from util import create_http_pool_manager
from ldclient.event_processor import DefaultEventProcessor

class LocustEventDispatcher(DefaultEventProcessor):
    def __init__(self, environment, config, http=None, dispatcher_class=None):
        http = create_http_pool_manager(environment, num_pools=1, verify_ssl=config.verify_ssl, target_base_uri=config.events_uri, force_proxy=config.http_proxy)
        super(LocustEventDispatcher, self).__init__(config, http=http, dispatcher_class=dispatcher_class)
