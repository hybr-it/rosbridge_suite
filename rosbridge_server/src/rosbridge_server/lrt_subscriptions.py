from __future__ import print_function

import sys
import uuid
import logging
# import profilehooks
import threading
import time
import six
import requests
from rdflib import Graph, Literal, URIRef, Namespace, RDF
from rdflib.namespace import XSD, DCTERMS
import six.moves.urllib as urllib
import lrt_debug_switch as debug_switch
import lrt_debug as debug
from lrt_rdf import hybrit_graph, SUBSCRIPTION, ROS, HYBRIT
import concurrent.futures
import atexit

logger = logging.getLogger(__name__)


class Handler(object):
    def __init__(self):
        """Subscription Handler
        """

        print("Initializing subscriptions...", file=sys.stderr)

        self._thread_lock = threading.RLock()
        self._thread = None
        self._stop_thread = False
        self._executor = concurrent.futures.ThreadPoolExecutor()
        self._futures_counter_lock = threading.Lock()
        self._num_futures = 0

        self.update_callback = None
        self.active_subscriptions = {}

    def __del__(self):
        debug.log("DESTROY HANDLER")
        self.destroy()

    def destroy(self):
        try:
            debug.log("SHUTDOWN HANDLER")
            self.stop(wait=False)
            debug.log("SHUTDOWN EXECUTOR")
            self._executor.shutdown()
        finally:
            self.stop()

    def _create_thread(self):
        debug.log("Starting subscriptions...")
        self._thread = threading.Thread(name='SubscriptionHandler', target=self._update_loop)
        self._thread.daemon = True

    @staticmethod
    def _notify_subscription(session, subscription, rdf):
        try:
            if not debug_switch.DEBUG_NO_NOTIFICATION_REQUESTS:
                response = session.post(subscription.callback_url, data=rdf,
                                        headers={'Content-Type': subscription.content_type})
                print("update_loop: response: {0}".format(response))
        except requests.exceptions.RequestException as ex:
            print("update_loop: Exception {0}: {1}".format(ex.__class__.__name__, ex), file=sys.stderr)

    def _decr_futures_counter(self, future):
        with self._futures_counter_lock:
            self._num_futures -= 1

    # @profilehooks.profile
    def _notify_subscriptions(self, session, subscriptions):
        for subscription in six.itervalues(subscriptions):
            if not subscription._callback:
                continue

            result = subscription._callback(subscription.last_time)
            if not result:
                print("skipped update")
                continue

            rdf, timestamp = result
            if not rdf:
                continue

            subscription.last_time = timestamp
            # print(timestamp)
            f = self._executor.submit(self._notify_subscription, session, subscription, rdf)
            with self._futures_counter_lock:
                self._num_futures += 1
            f.add_done_callback(self._decr_futures_counter)
            # self._notify_subscription(session, subscription, rdf)

    def _update_loop(self):
        with requests.Session() as s:
            while not self._stop_thread:

                with self._futures_counter_lock:
                    n = len(self.active_subscriptions)
                    if self._num_futures > n * 5:
                        debug.log("TOO MANY FUTURES", self._num_futures, "NUM_SUBSCR", n)
                        time.sleep(0)
                        continue
                    # else:
                    #    debug.log("NUM_FUTURES", self._num_futures)

                with self._thread_lock:
                    update_callback = self.update_callback
                    active_subscriptions = None
                    if self.active_subscriptions:
                        active_subscriptions = self.active_subscriptions.copy()

                t = time.clock()
                if update_callback:
                    update_callback()
                if active_subscriptions:
                    try:
                        self._notify_subscriptions(s, active_subscriptions)
                    except RuntimeError as e:
                        print("Subscriptions Loop: Cannot schedule new requests", file=sys.stderr)
                        continue

                dt = time.clock() - t
                num_subscr = 0
                sps = 0
                if active_subscriptions:
                    sps = len(active_subscriptions) / dt
                    num_subscr = len(active_subscriptions)
                debug.log("Delta", dt * 1000, "ms", "SPS", sps, "NUM_SUBSCR", num_subscr, file=sys.stderr)

                # time.sleep(0)

    def start(self):
        with self._thread_lock:
            if self._thread is not None:
                return
            self._create_thread()
            self._stop_thread = False
            self._thread.start()

    def stop(self, wait=True):
        with self._thread_lock:
            if self._thread is not None:
                self._stop_thread = True
                if wait:
                    self._thread.join()
                    t = self._thread
                    self._thread = None
                    del t

    def set_update_callback(self, cb):
        if cb is not None and not callable(cb):
            raise Exception("Callback must be callable or None")
        with self._thread_lock:
            old_cb = self.update_callback
            self.update_callback = cb
            return old_cb

    def get_update_callback(self):
        with self._thread_lock:
            return self.update_callback

    def clear_subscriptions(self):
        with self._thread_lock:
            self.active_subscriptions = {}

    def get_subscription_by_id(self, id):
        with self._thread_lock:
            return self.active_subscriptions.get(id)

    def get_subscriptions(self):
        with self._thread_lock:
            return self.active_subscriptions.copy()

    def register_subscription(self, s):
        with self._thread_lock:
            key = s.id
            if key not in self.active_subscriptions:
                self.active_subscriptions[key] = s
                return True
            return False

    def unregister_subscription(self, s):
        with self._thread_lock:
            key = s.id
            if key in self.active_subscriptions:
                del self.active_subscriptions[key]
                return True
            return False

    def is_registered_subscription(self, s):
        with self._thread_lock:
            return s.id in self.active_subscriptions


_HANDLER = Handler()


def _on_exit():
    if _HANDLER is not None:
        _HANDLER.destroy()


atexit.register(_on_exit)


def set_update_callback(cb):
    return _HANDLER.set_update_callback(cb)


def get_update_callback():
    return _HANDLER.get_update_callback()


def clear_subscriptions():
    _HANDLER.clear_subscriptions()


def filter_subscriptions(filter_func):
    for s in six.itervalues(_HANDLER.get_subscriptions()):
        if filter_func(s):
            yield s


def get_subscription_by_id(id):
    return _HANDLER.get_subscription_by_id(id)


def start_loop():
    _HANDLER.start()


class Subscription(object):
    def __init__(self, callback=None, context=None):
        self._id = uuid.uuid4()
        self._callback = callback
        self._context = context
        self.last_time = 0

    @property
    def id(self):
        return str(self._id)

    @property
    def context(self):
        return self._context

    def register(self):
        return _HANDLER.register_subscription(self)

    def unregister(self):
        return _HANDLER.unregister_subscription(self)

    def is_registered(self):
        return _HANDLER.is_registered_subscription(self)

    def to_rdf(self, resource_uri=None, graph=None):
        graph = hybrit_graph(graph)
        return graph

    def rdf_node(self):
        return None


class WebhookSubscription(Subscription):
    def __init__(self, target_resource, callback_url, content_type="text/turtle", callback=None, context=None):
        super(WebhookSubscription, self).__init__(callback=callback, context=context)
        self.target_resource = target_resource
        self.target_parsed_url = urllib.parse.urlsplit(target_resource)
        self.callback_url = callback_url
        self.content_type = content_type

    @property
    def target_path(self):
        return self.target_parsed_url.path

    def to_rdf(self, base_uri=None, graph=None):
        graph = hybrit_graph(graph)

        if base_uri:
            target_resource = urllib.parse.urljoin(base_uri, url=self.target_parsed_url[2])
        else:
            target_resource = self.target_resource

        subscr_node = self.rdf_node()
        graph.add((subscr_node, RDF.type, HYBRIT.Subscription))
        graph.add((subscr_node, RDF.type, HYBRIT.WebCallback))
        graph.add((subscr_node, SUBSCRIPTION.type, Literal("webhook")))
        graph.add((subscr_node, HYBRIT.onResource, URIRef(target_resource)))
        graph.add((subscr_node, SUBSCRIPTION.targetResource, URIRef(target_resource)))
        graph.add((subscr_node, HYBRIT.mediaType, Literal(self.content_type)))
        graph.add((subscr_node, HYBRIT.callbackUrl, Literal(self.callback_url, datatype=XSD.anyURI)))
        graph.add((subscr_node, DCTERMS.identifier, Literal(str(self.id))))

        return graph

    def rdf_node(self, base_uri=None):
        if base_uri:
            target_resource = urllib.parse.urljoin(base_uri, url=self.target_parsed_url[2])
        else:
            target_resource = self.target_resource
        subscr_uri = urllib.parse.urljoin(target_resource, 'subscriptions/{}'.format(self.id))
        return URIRef(subscr_uri)

    def notify(self, data, session=None):
        if not self.callback_url:
            return
        try:
            if not debug_switch.DEBUG_NO_NOTIFICATION_REQUESTS:
                if not session:
                    post = requests.post
                else:
                    post = session.post
                response = post(self.callback_url, data=data,
                                headers={'Content-Type': self.content_type})
                # print("notify: response: {0}".format(response)) DEBUG
        except requests.exceptions.RequestException as ex:
            print("notify: Exception {0}: {1}".format(ex.__class__.__name__, ex), file=sys.stderr)


class WebsocketSubscription(Subscription):
    def __init__(self, target_resource, websocket_url, content_type="text/turtle", callback=None,
                 notification_callback=None, context=None):
        super(WebsocketSubscription, self).__init__(callback=callback, context=context)
        self.target_resource = target_resource
        self.target_parsed_url = urllib.parse.urlsplit(target_resource)
        self.websocket_url = websocket_url
        self.content_type = content_type
        self.notification_callback = notification_callback

    @property
    def target_path(self):
        return self.target_parsed_url.path

    def to_rdf(self, base_uri=None, graph=None):
        graph = hybrit_graph(graph)

        if base_uri:
            target_resource = urllib.parse.urljoin(base_uri, url=self.target_parsed_url[2])
        else:
            target_resource = self.target_resource

        subscr_node = self.rdf_node()
        graph.add((subscr_node, RDF.type, HYBRIT.Subscription))
        graph.add((subscr_node, RDF.type, HYBRIT.WebsocketCallback))
        graph.add((subscr_node, SUBSCRIPTION.type, Literal("websocket")))
        graph.add((subscr_node, HYBRIT.onResource, URIRef(target_resource)))
        graph.add((subscr_node, SUBSCRIPTION.targetResource, URIRef(target_resource)))
        graph.add((subscr_node, HYBRIT.mediaType, Literal(self.content_type)))
        graph.add((subscr_node, HYBRIT.websocketUrl, Literal(self.websocket_url, datatype=XSD.anyURI)))
        graph.add((subscr_node, DCTERMS.identifier, Literal(str(self.id))))

        return graph

    def rdf_node(self, base_uri=None):
        if base_uri:
            target_resource = urllib.parse.urljoin(base_uri, url=self.target_parsed_url[2])
        else:
            target_resource = self.target_resource
        subscr_uri = urllib.parse.urljoin(target_resource, 'subscriptions/{}'.format(self.id))
        return URIRef(subscr_uri)

    def notify(self, data, *args, **kwargs):
        if self.notification_callback:
            self.notification_callback(data, *args, **kwargs)
