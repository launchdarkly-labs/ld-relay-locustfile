import threading
import traceback

from ldclient import LDClient

from config_mobile import MobileConfig
from feature_requester_mobile import FeatureRequesterImpl
from ldclient.feature_store import _FeatureStoreDataSetSorter
from ldclient.flag import EvaluationDetail, error_reason
from ldclient.flags_state import FeatureFlagsState
from ldclient.impl.event_factory import _EventFactory
from ldclient.impl.stubs import NullUpdateProcessor
from ldclient.interfaces import FeatureStore
from ldclient.polling import PollingUpdateProcessor
from streaming_mobile import MobileStreamingUpdateProcessor as StreamingUpdateProcessor
from ldclient.util import log
from ldclient.versioned_data_kind import FEATURES
from client_evaluate import evaluate

from threading import Lock

class _FeatureStoreClientWrapper(FeatureStore):
    """Provides additional behavior that the client requires before or after feature store operations.
    Currently this just means sorting the data set for init(). In the future we may also use this
    to provide an update listener capability.
    """

    def __init__(self, store):
        self.store = store

    def init(self, all_data):
        return self.store.init(_FeatureStoreDataSetSorter.sort_all_collections(all_data))

    def get(self, kind, key, callback):
        return self.store.get(kind, key, callback)

    def all(self, kind, callback):
        return self.store.all(kind, callback)

    def delete(self, kind, key, version):
        return self.store.delete(kind, key, version)

    def upsert(self, kind, item):
        return self.store.upsert(kind, item)

    @property
    def initialized(self):
        return self.store.initialized

class MobileLDClient(LDClient):
  def __init__(self, environment, user, sdk_key=None, config=None, start_wait=5):
        """Constructs a new LDClient instance.
        :param string sdk_key: the SDK key for your LaunchDarkly environment
        :param ldclient.config.Config config: optional custom configuration
        :param float start_wait: the number of seconds to wait for a successful connection to LaunchDarkly
        """

        self._environment = environment

        if user is None:
          raise Exception("user is required for mobile sdk")

        if config is not None and config.sdk_key is not None and sdk_key is not None:
            raise Exception("LaunchDarkly client init received both sdk_key and config with sdk_key. "
                            "Only one of either is expected")

        if sdk_key is not None:
            log.warning("Deprecated sdk_key argument was passed to init. Use config object instead.")
            self._config = MobileConfig(sdk_key=sdk_key)
        else:
            self._config = config.copy_with_new_user(user) or MobileConfig.default().copy_with_new_user(user=user)

        self._config._validate()

        self._event_processor = None
        self._lock = Lock()
        self._event_factory_default = _EventFactory(False)
        self._event_factory_with_reasons = _EventFactory(True)

        self._store = _FeatureStoreClientWrapper(self._config.feature_store)
        """ :type: FeatureStore """

        if self._config.offline:
            log.info("Started LaunchDarkly Client in offline mode")

        if self._config.use_ldd:
            log.info("Started LaunchDarkly Client in LDD mode")

        self._event_processor = self._make_event_processor(self._config)
        update_processor_ready = threading.Event()
        self._update_processor = self._make_update_processor(self._config, self._store, update_processor_ready)
        self._update_processor.start()

        if start_wait > 0 and not self._config.offline and not self._config.use_ldd:
            log.info("Waiting up to " + str(start_wait) + " seconds for LaunchDarkly client to initialize...")
            update_processor_ready.wait(start_wait)

        if self._update_processor.initialized() is True:
            log.info("Started LaunchDarkly Client: OK")

        else:
            log.warning("Initialization timeout exceeded for LaunchDarkly Client or an error occurred. "
                     "Feature Flags may not yet be available.")

  def _make_update_processor(self, config, store, ready):
      if config.update_processor_class:
          log.info("Using user-specified update processor: " + str(config.update_processor_class))
          return config.update_processor_class(config, store, ready)

      if config.offline or config.use_ldd:
          return NullUpdateProcessor(config, store, ready)

      if config.feature_requester_class:
          feature_requester = config.feature_requester_class(config)
      else:
          feature_requester = FeatureRequesterImpl(self._environment, config)
      """ :type: FeatureRequester """

      if config.stream:
          return StreamingUpdateProcessor(self._environment, config, feature_requester, store, ready)

      log.info("Disabling streaming API")
      log.warning("You should only disable the streaming API if instructed to do so by LaunchDarkly support")
      return PollingUpdateProcessor(config, feature_requester, store, ready)

  def track(self, event_name, data=None, metric_value=None):
        """Tracks that a user performed an event.
        LaunchDarkly automatically tracks pageviews and clicks that are specified in the Goals
        section of the dashboard. This can be used to track custom goals or other events that do
        not currently have goals.
        :param string event_name: the name of the event, which may correspond to a goal in A/B tests
        :param dict user: the attributes of the user
        :param data: optional additional data associated with the event
        :param metric_value: a numeric value used by the LaunchDarkly experimentation feature in
          numeric custom metrics. Can be omitted if this event is used by only non-numeric metrics.
          This field will also be returned as part of the custom event for Data Export.
        """
        user = self._config.user
        if user is None or user.get('key') is None:
            log.warning("Missing user or user key when calling track().")
        else:
            self._send_event(self._event_factory_default.new_custom_event(event_name, user, data, metric_value))

  def identify(self, user):
        """Registers the user.
        This simply creates an analytics event that will transmit the given user properties to
        LaunchDarkly, so that the user will be visible on your dashboard even if you have not
        evaluated any flags for that user. It has no other effect.
        :param dict user: attributes of the user to register
        """
        if user is None or user.get('key') is None:
            raise Exception("Missing user or user key when calling identify().")
        else:
            self._send_event(self._event_factory_default.new_identify_event(user))
            self.close()
            self.__init__(self._environment, user, config=self._config)

  def toggle(self, key, default):
        """Deprecated synonym for :func:`variation()`.
        .. deprecated:: 2.0.0
        """
        log.warning("Deprecated method: toggle() called. Use variation() instead.")
        return self.variation(key, default)

  def variation(self, key, default):
        """Determines the variation of a feature flag for a user.
        :param string key: the unique key for the feature flag
        :param dict user: a dictionary containing parameters for the end user requesting the flag
        :param object default: the default value of the flag, to be used if the value is not
          available from LaunchDarkly
        :return: one of the flag's variation values, or the default value
        """
        return self._evaluate_internal(key, default, self._event_factory_default).value

  def variation_detail(self, key, default):
        """Determines the variation of a feature flag for a user, like :func:`variation()`, but also
        provides additional information about how this value was calculated, in the form of an
        :class:`ldclient.flag.EvaluationDetail` object.

        Calling this method also causes the "reason" data to be included in analytics events,
        if you are capturing detailed event data for this flag.

        :param string key: the unique key for the feature flag
        :param dict user: a dictionary containing parameters for the end user requesting the flag
        :param object default: the default value of the flag, to be used if the value is not
          available from LaunchDarkly
        :return: an object describing the result
        :rtype: EvaluationDetail
        """
        return self._evaluate_internal(key, default, self._event_factory_with_reasons)

  def _evaluate_internal(self, key, default, event_factory):
        user = self._config.user
        default = self._config.get_default(key, default)

        if self._config.offline:
            return EvaluationDetail(default, None, error_reason('CLIENT_NOT_READY'))

        if not self.is_initialized():
            if self._store.initialized:
                log.warning("Feature Flag evaluation attempted before client has initialized - using last known values from feature store for feature key: " + key)
            else:
                log.warning("Feature Flag evaluation attempted before client has initialized! Feature store unavailable - returning default: "
                         + str(default) + " for feature key: " + key)
                reason = error_reason('CLIENT_NOT_READY')
                self._send_event(event_factory.new_unknown_flag_event(key, user, default, reason))
                return EvaluationDetail(default, None, reason)

        if user is not None and user.get('key', "") == "":
            raise ValueError('user must have a key')

        try:
            flag = self._store.get(FEATURES, key, lambda x: x)
        except Exception as e:
            log.error("Unexpected error while retrieving feature flag \"%s\": %s" % (key, repr(e)))
            log.debug(traceback.format_exc())
            reason = error_reason('EXCEPTION')
            self._send_event(event_factory.new_unknown_flag_event(key, user, default, reason))
            return EvaluationDetail(default, None, reason)
        if not flag:
            reason = error_reason('FLAG_NOT_FOUND')
            self._send_event(event_factory.new_unknown_flag_event(key, user, default, reason))
            return EvaluationDetail(default, None, reason)
        else:

            try:
                result = evaluate(flag)
                for event in result.events or []:
                    self._send_event(event)
                detail = result.detail
                if detail.is_default_value():
                    detail = EvaluationDetail(default, None, detail.reason)
                self._send_event(event_factory.new_eval_event(flag, user, detail, default))
                return detail
            except Exception as e:
                log.error("Unexpected error while evaluating feature flag \"%s\": %s" % (key, repr(e)))
                log.debug(traceback.format_exc())
                reason = error_reason('EXCEPTION')
                self._send_event(event_factory.new_default_event(flag, user, default, reason))
                return EvaluationDetail(default, None, reason)
  def all_flags(self):
        """Returns all feature flag values for the given user.

        This method is deprecated - please use :func:`all_flags_state()` instead. Current versions of the
        client-side SDK will not generate analytics events correctly if you pass the result of ``all_flags``.
        :param dict user: the end user requesting the feature flags
        :return: a dictionary of feature flag keys to values; returns None if the client is offline,
          has not been initialized, or the user is None or has no key
        :rtype: dict
        """
        state = self.all_flags_state()
        if not state.valid:
            return None

  def all_flags_state(self, **kwargs):
        """Returns an object that encapsulates the state of all feature flags for a given user,
        including the flag values and also metadata that can be used on the front end. See the
        JavaScript SDK Reference Guide on
        `Bootstrapping <https://docs.launchdarkly.com/docs/js-sdk-reference#section-bootstrapping>`_.

        This method does not send analytics events back to LaunchDarkly.
        :param dict user: the end user requesting the feature flags
        :param kwargs: optional parameters affecting how the state is computed - see below
        :Keyword Arguments:
          * **client_side_only** (*boolean*) --
            set to True to limit it to only flags that are marked for use with the client-side SDK
            (by default, all flags are included)
          * **with_reasons** (*boolean*) --
            set to True to include evaluation reasons in the state (see :func:`variation_detail()`)
          * **details_only_for_tracked_flags** (*boolean*) --
            set to True to omit any metadata that is normally only used for event generation, such
            as flag versions and evaluation reasons, unless the flag has event tracking or debugging
            turned on
        :return: a FeatureFlagsState object (will never be None; its ``valid`` property will be False
          if the client is offline, has not been initialized, or the user is None or has no key)
        :rtype: FeatureFlagsState
        """
        user = self._config.user
        if self._config.offline:
            log.warning("all_flags_state() called, but client is in offline mode. Returning empty state")
            return FeatureFlagsState(False)

        if not self.is_initialized():
            if self._store.initialized:
                log.warning("all_flags_state() called before client has finished initializing! Using last known values from feature store")
            else:
                log.warning("all_flags_state() called before client has finished initializing! Feature store unavailable - returning empty state")
                return FeatureFlagsState(False)

        if user is None or user.get('key') is None:
            log.warning("User or user key is None when calling all_flags_state(). Returning empty state.")
            return FeatureFlagsState(False)

        state = FeatureFlagsState(True)
        with_reasons = kwargs.get('with_reasons', False)
        details_only_if_tracked = kwargs.get('details_only_for_tracked_flags', False)
        try:
            flags_map = self._store.all(FEATURES, lambda x: x)
            if flags_map is None:
                raise ValueError("feature store error")
        except Exception as e:
            log.error("Unable to read flags for all_flag_state: %s" % repr(e))
            return FeatureFlagsState(False)

        for key, flag in flags_map.items():
            try:
                detail = evaluate(flag).detail
                state.add_flag(flag, detail.value, detail.variation_index,
                    detail.reason if with_reasons else None, details_only_if_tracked)
            except Exception as e:
                log.error("Error evaluating flag \"%s\" in all_flags_state: %s" % (key, repr(e)))
                log.debug(traceback.format_exc())
                reason = {'kind': 'ERROR', 'errorKind': 'EXCEPTION'}
                state.add_flag(flag, None, None, reason if with_reasons else None, details_only_if_tracked)

        return state
  def secure_mode_hash(self):
        """Computes an HMAC signature of a user signed with the client's SDK key,
        for use with the JavaScript SDK.
        For more information, see the JavaScript SDK Reference Guide on
        `Secure mode <https://github.com/launchdarkly/js-client#secure-mode>`_.

        :param dict user: the attributes of the user
        :return: a hash string that can be passed to the front end
        :rtype: string
        """
        raise Exception('not supported on mobile client')

__all__ = [ 'MobileLDClient', 'MobileConfig' ]
