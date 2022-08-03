from ldclient.config import Config
from ldclient.user_filter import UserFilter
import json
from base64 import urlsafe_b64encode
from ldclient.event_processor import DefaultEventProcessor
from ldclient.feature_store import InMemoryFeatureStore

class MobileConfig(Config):
  def __init__(self,sdk_key=None, user=None, use_report=False, evaluation_reasons=False,
                 base_uri='https://app.launchdarkly.com',
                 events_uri='https://mobile.launchdarkly.com',
                 connect_timeout=10,
                 read_timeout=15,
                 events_max_pending=10000,
                 flush_interval=5,
                 stream_uri='https://clientstream.launchdarkly.com',
                 stream=True,
                 verify_ssl=True,
                 defaults=None,
                 send_events=None,
                 events_enabled=True,
                 update_processor_class=None,
                 poll_interval=30,
                 use_ldd=False,
                 feature_store=None,
                 feature_requester_class=None,
                 event_processor_class=None,
                 private_attribute_names=(),
                 all_attributes_private=False,
                 offline=False,
                 user_keys_capacity=1000,
                 user_keys_flush_interval=300,
                 inline_users_in_events=False,
                 http_proxy=None):

    self.__sdk_key = sdk_key

    if defaults is None:
      defaults = {}

    self.__base_uri = base_uri.rstrip('\\')
    self.__events_uri = events_uri.rstrip('\\')
    self.__stream_uri = stream_uri.rstrip('\\')
    self.__update_processor_class = update_processor_class
    self.__stream = stream
    self.__poll_interval = max(poll_interval, 30)
    self.__use_ldd = use_ldd
    self.__feature_store = InMemoryFeatureStore() if not feature_store else feature_store
    self.__event_processor_class = DefaultEventProcessor if not event_processor_class else event_processor_class
    self.__feature_requester_class = feature_requester_class
    self.__connect_timeout = connect_timeout
    self.__read_timeout = read_timeout
    self.__events_max_pending = events_max_pending
    self.__flush_interval = flush_interval
    self.__verify_ssl = verify_ssl
    self.__defaults = defaults
    if offline is True:
        send_events = False
    self.__send_events = events_enabled if send_events is None else send_events
    self.__private_attribute_names = private_attribute_names
    self.__all_attributes_private = all_attributes_private
    self.__offline = offline
    self.__user_keys_capacity = user_keys_capacity
    self.__user_keys_flush_interval = user_keys_flush_interval
    self.__inline_users_in_events = inline_users_in_events
    self.__http_proxy = http_proxy

    self.__use_report = use_report
    self.__evaluation_reasons = evaluation_reasons
    self.__user = user
    self.__user_filter = UserFilter(self)
    if user is not None:
      self.__user_filtered = self.__user_filter.filter_user_props(user)
      self.__user_json = json.dumps(self.__user_filtered).encode('utf-8')
      self.__user_b64 = urlsafe_b64encode(self.__user_json).decode('utf-8')


  def copy_with_new_sdk_key(self, new_sdk_key):
    """Returns a new ``Config`` instance that is the same as this one, except for having a different SDK key.
    :param string new_sdk_key: the new SDK key
    :rtype: ldclient.config.Config
    """
    return MobileConfig(sdk_key=new_sdk_key,
                  base_uri=self.__base_uri,
                  events_uri=self.__events_uri,
                  connect_timeout=self.__connect_timeout,
                  read_timeout=self.__read_timeout,
                  events_max_pending=self.__events_max_pending,
                  flush_interval=self.__flush_interval,
                  stream_uri=self.__stream_uri,
                  stream=self.__stream,
                  verify_ssl=self.__verify_ssl,
                  defaults=self.__defaults,
                  send_events=self.__send_events,
                  update_processor_class=self.__update_processor_class,
                  poll_interval=self.__poll_interval,
                  use_ldd=self.__use_ldd,
                  feature_store=self.__feature_store,
                  feature_requester_class=self.__feature_requester_class,
                  event_processor_class=self.__event_processor_class,
                  private_attribute_names=self.__private_attribute_names,
                  all_attributes_private=self.__all_attributes_private,
                  offline=self.__offline,
                  user_keys_capacity=self.__user_keys_capacity,
                  user_keys_flush_interval=self.__user_keys_flush_interval,
                  inline_users_in_events=self.__inline_users_in_events,
                  use_report=self.__use_report,
                  evaluation_reasons=self.__evaluation_reasons,
                  user=self.__user)
  def copy_with_new_user(self, new_user, new_sdk_key=None):
    """Returns a new ``Config`` instance that is the same as this one, except for having a different SDK key.
    :param string new_sdk_key: the new SDK key
    :rtype: ldclient.config.Config
    """
    key = new_sdk_key or self.sdk_key

    return MobileConfig(sdk_key=key,
                  base_uri=self.__base_uri,
                  events_uri=self.__events_uri,
                  connect_timeout=self.__connect_timeout,
                  read_timeout=self.__read_timeout,
                  events_max_pending=self.__events_max_pending,
                  flush_interval=self.__flush_interval,
                  stream_uri=self.__stream_uri,
                  stream=self.__stream,
                  verify_ssl=self.__verify_ssl,
                  defaults=self.__defaults,
                  send_events=self.__send_events,
                  update_processor_class=self.__update_processor_class,
                  poll_interval=self.__poll_interval,
                  use_ldd=self.__use_ldd,
                  feature_store=self.__feature_store,
                  feature_requester_class=self.__feature_requester_class,
                  event_processor_class=self.__event_processor_class,
                  private_attribute_names=self.__private_attribute_names,
                  all_attributes_private=self.__all_attributes_private,
                  offline=self.__offline,
                  user_keys_capacity=self.__user_keys_capacity,
                  user_keys_flush_interval=self.__user_keys_flush_interval,
                  inline_users_in_events=self.__inline_users_in_events,
                  use_report=self.__use_report,
                  evaluation_reasons=self.__evaluation_reasons,
                  user=new_user)


  @property
  def evaluation_reasons(self):
    return self.__evaluation_reasons
  @property
  def use_report(self):
    return self.__use_report
  @property
  def user(self):
    return self.__user
  @property
  def user_b64(self):
    return self.__user_b64
  @property
  def user_json(self):
    return self.__user_json


  # for internal use only - probably should be part of the client logic
  def get_default(self, key, default):
    return default if key not in self.__defaults else self.__defaults[key]

  @property
  def sdk_key(self):
      return self.__sdk_key

  @property
  def base_uri(self):
      return self.__base_uri

  # for internal use only - also no longer used, will remove
  @property
  def get_latest_flags_uri(self):
      return self.__base_uri + '/msdk/evalx'

  # for internal use only - should construct the URL path in the events code, not here
  @property
  def events_uri(self):
      return self.__events_uri + '/mobile/events/bulk'

  # for internal use only
  @property
  def stream_base_uri(self):
      return self.__stream_uri

  # for internal use only - should construct the URL path in the streaming code, not here
  @property
  def stream_uri(self):
      return self.__stream_uri + '/msdk/evalx'

  @property
  def update_processor_class(self):
      return self.__update_processor_class

  @property
  def stream(self):
      return self.__stream

  @property
  def poll_interval(self):
      return self.__poll_interval

  @property
  def use_ldd(self):
      return self.__use_ldd

  @property
  def feature_store(self):
      return self.__feature_store

  @property
  def event_processor_class(self):
      return self.__event_processor_class

  @property
  def feature_requester_class(self):
      return self.__feature_requester_class

  @property
  def connect_timeout(self):
      return self.__connect_timeout

  @property
  def read_timeout(self):
      return self.__read_timeout

  @property
  def events_enabled(self):
      return self.__send_events

  @property
  def send_events(self):
      return self.__send_events

  @property
  def events_max_pending(self):
      return self.__events_max_pending

  @property
  def flush_interval(self):
      return self.__flush_interval

  @property
  def verify_ssl(self):
      return self.__verify_ssl

  @property
  def private_attribute_names(self):
      return list(self.__private_attribute_names)

  @property
  def all_attributes_private(self):
      return self.__all_attributes_private

  @property
  def offline(self):
      return self.__offline

  @property
  def user_keys_capacity(self):
      return self.__user_keys_capacity

  @property
  def user_keys_flush_interval(self):
      return self.__user_keys_flush_interval

  @property
  def inline_users_in_events(self):
      return self.__inline_users_in_events

  @property
  def http_proxy(self):
      return self.__http_proxy



  def _validate(self):
      if self.offline is False and self.sdk_key is None or self.sdk_key == '':
          log.warning("Missing or blank sdk_key.")
