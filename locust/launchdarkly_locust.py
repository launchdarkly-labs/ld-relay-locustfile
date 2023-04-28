import os
import inspect
import copy

from locust import Locust

from ldclient import LDClient, Config
from ldclient_mobile import MobileLDClient, MobileConfig

from locust_event_dispatcher import LocustEventDispatcher
from locust_feature_requester import FeatureRequesterImpl as LocustServerFeatureRequester
from locust_streaming import StreamingUpdateProcessor as LocustStreamingProcessor


class LaunchDarklyLocust(Locust):
    sdk_key = os.environ.get('LAUNCHDARKLY_SDK_KEY')
    default_user = {"anonymous": True, "key": "anonymous"}
    config_class = Config
    client_class = LDClient
    base_uri = os.environ.get('LAUNCHDARKLY_BASE_URI')
    events_uri = os.environ.get('LAUNCHDARKLY_EVENTS_URI')
    stream_uri = os.environ.get('LAUNCHDARKLY_STREAM_URI')
    send_events = False  # https://docs.launchdarkly.com/guides/flags/testing-code#managing-test-data-in-your-production-environment
    event_processor_class = LocustEventDispatcher
    feature_requester_class = LocustServerFeatureRequester
    update_processor_class = LocustStreamingProcessor

    def __init__(self, *args, **kwargs):
        super(LaunchDarklyLocust, self).__init__(*args, **kwargs)
        self._ldclient = None
        self._user = None
        if self.base_uri is None:
            self.base_uri = self.host
        if self.events_uri is None:
            self.events_uri = self.host
        if self.stream_uri is None:
            self.stream_uri = self.host


    # override this method in your subclass 
    # if you want to generate random users for each Locust instance
    def generate_user(self):
      return self.default_user

    # you can override this property if you to generate random users
    # for each locust instance    
    @property
    def user(self):
      if self._user == None:
        self._user = copy.deepcopy(self.generate_user())
      return self._user

    def make_config(self):
        # do you believe in magic?
        # grab kwargs from our class attributes
        args = { 
            arg: getattr(self, arg) for arg in \
                    filter(lambda x: hasattr(self, x) and x not in ['self', 'return', 'user'] ,
                        inspect.getfullargspec(self.config_class.__init__).args)
        }
        
        return self.config_class(**args)

    def make_client(self):
        return self.client_class(config=self.make_config())

    # magic property that will auto-initialize an ldclient when accessed
    @property
    def ldclient(self):
        if self._ldclient is None:
          self._ldclient = self.make_client()
        return self._ldclient
        
    def close_client(self):
        try:
            if self._ldclient is not None:
                self._ldclient.close()
        except Exception:
            pass
        finally:
            self._ldclient = None

class LaunchDarklyMobileLocust(LaunchDarklyLocust):
    sdk_key = os.environ.get('LAUNCHDARKLY_MOBILE_KEY')
    stream_uri = os.environ.get('LAUNCHDARKLY_MOBILE_STREAM_URI')
    client_class = MobileLDClient
    config_class = MobileConfig
    # default classes for our mobile client are already instrumented for locust
    feature_requester_class=None 
    update_processor_class=None

    def make_client(self, user=None):
        if user is None:
          user = self.user
        return self.client_class(user, config=self.make_config())