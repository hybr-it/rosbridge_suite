from __future__ import print_function
import logging
import sys
import six
import json
import rospy
import tornado
import tornado.web
from tornado.ioloop import IOLoop
from tornado.websocket import WebSocketHandler
import uuid
import rdflib
import rdflib.namespace
from rdflib.namespace import DCTERMS, XSD
from rosbridge_library.util import rdfutils
from rosapi.glob_helper import get_globs, topics_glob, services_glob, params_glob
from rosapi import proxy, objectutils, params
import lrt_debug as debug
import lrt_debug_switch as debug_switch
import lrt_subscriptions
from lrt_rdf import hybrit_graph, LDP, ROS, HYBRIT
from werkzeug.routing import Map, Rule, HTTPException, NotFound, RequestRedirect
from functools import partial
from rosbridge_library.internal import ros_loader
from rosbridge_library.internal.publishers import manager as publisher_manager
from rosbridge_library.internal.subscribers import manager as subscriber_manager
from rosbridge_library.capabilities.subscribe import Subscription
from rosbridge_library.capabilities.advertise import Registration
from collections import defaultdict

UUID_URI = 'http://{}'.format(uuid.uuid4())

logger = logging.getLogger(__name__)

HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_UNSUPPORTED_MEDIA_TYPE = 415
HTTP_INTERNAL_SERVER_ERROR = 500
HTTP_NOT_IMPLEMENTED = 501


def create_graph(base_name=UUID_URI):
    text = """
@base <{root}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ros: <http://ros.org/#> .
@prefix rosbridge: <http://ros.org/rosbridge#> .
@prefix xml: <http://www.w3.org/XML/1998/namespace> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix hybrit: <https://hybr-it-projekt.de/ns/hybr-it#> .
@prefix ldp: <http://www.w3.org/ns/ldp#> .
@prefix dcterms: <http://purl.org/dc/terms/> .

<>
    rdf:type hybrit:RoboticThing ;
    hybrit:topics <topics> .

<topics>
    rdf:type ldp:BasicContainer ;
    rdf:type ldp:Container .
    """.format(root=rdfutils.add_slash(UUID_URI))
    g = hybrit_graph().parse(data=text, format='turtle')
    return g


LRT_GRAPH = create_graph()


def join_paths(p1, p2):
    if not p1:
        return p2
    if not p2:
        return p1
    if not p1.endswith('/'):
        p1 += '/'
    if p2.startswith('/'):
        p2 = p2[1:]
    return p1 + p2


def copy_end_slash(p1, p2):
    """Add slash to p1 if p2 ends with slash,
    Remove slash from p1 if p2 does not end with slash
    """
    if p2:
        if p2.endswith('/'):
            return rdfutils.add_slash(p1)
        else:
            return p1.rstrip('/')
    return p1


def slash_at_start(s):
    return s if s.startswith('/') else '/' + s


def slash_at_end(s):
    return s if s.endswith('/') else s + '/'


def ros_message_to_rdf(msg, rdf_content_type=None):
    rdfutils.add_jsonld_context_to_ros_message(msg)

    result = None
    if rdf_content_type and rdf_content_type != "application/ld+json":
        rdf_graph = rdflib.Graph().parse(data=json.dumps(msg), format='json-ld')
        result, content_type = rdfutils.serialize_rdf_graph(rdf_graph, rdf_content_type)
    else:
        result = json.dumps(msg)

    return result


class LRTState(object):
    """LinkedRoboticThing state kept between requests"""

    def __init__(self, client_id=None):
        self.client_id = client_id

        self._on_destroy_called = False

        # Save the topics that are published on for the purposes of unregistering
        self._published = {}

        # Maps topic names to subscriptions
        self._subscriptions = {}

        # Advertised topics
        self._registrations = {}

        # Maps topic names to webhooks
        self._webhooks = defaultdict(list)

    def __del__(self):
        self.destroy()

    def destroy(self):
        if not self._on_destroy_called:
            self._on_destroy_called = True
            self.on_destroy()

    def on_destroy(self):
        for topic in self._published:
            publisher_manager.unregister(self.client_id, topic)
        self._published.clear()
        for subscription in six.itervalues(self._subscriptions):
            subscription.unregister()
        self._subscriptions.clear()
        for registration in six.itervalues(self._registrations):
            registration.unregister()
        self._registrations.clear()
        for topic_webhooks in six.itervalues(self._webhooks):
            for webhook in topic_webhooks:
                webhook.unregister()
        self._webhooks.clear()

    def subscribe(self, topic, sid=None, msg_type=None, throttle_rate=0,
                  queue_length=0, fragment_size=None, compression="none", options=None):
        if topic not in self._subscriptions:
            cb = partial(self.notify_topic_subscribers, topic)
            self._subscriptions[topic] = Subscription(self.client_id, topic, cb)

        # Register the subscriber
        _options = {}
        if options is not None:
            _options.update(options)
        _options["add_ros_type_to_message"] = True
        self._subscriptions[topic].subscribe(sid=sid, msg_type=msg_type, throttle_rate=throttle_rate,
                                             queue_length=queue_length, fragment_size=fragment_size,
                                             compression=compression, options=_options)

        logger.info("Subscribed to %s", topic)

    def unsubscribe(self, topic, sid=None):
        if topic not in self._subscriptions:
            return
        self._subscriptions[topic].unsubscribe(sid)

        if self._subscriptions[topic].is_empty():
            self._subscriptions[topic].unregister()
            del self._subscriptions[topic]
        logger.info("Unsubscribed from %s", topic)

    def advertise(self, topic, msg_type, aid=None, latch=False, queue_size=100):
        # Create the Registration if one doesn't yet exist
        if not topic in self._registrations:
            self._registrations[topic] = Registration(self.client_id, topic)

        # Register, propagating any exceptions
        self._registrations[topic].register_advertisement(msg_type, aid, latch, queue_size)

    def unadvertise(self, topic, aid=None):
        # Now unadvertise the topic
        if topic not in self._registrations:
            return False
        self._registrations[topic].unregister_advertisement(aid)

        # Check if the registration is now finished with
        if self._registrations[topic].is_empty():
            self._registrations[topic].unregister()
            del self._registrations[topic]
        return True

    def notify_topic_subscribers(self, topic, message, fragment_size=None, compression="none"):
        # print("notify_topic_subscribers(%r, %r)" % (topic, message))  # DEBUG
        serialized_messages = {}  # Cache serialized messages by content type
        for webhook in self._webhooks[topic]:
            content_type = webhook.content_type
            smsg = serialized_messages.get(content_type, None)
            if smsg is None:
                try:
                    smsg = ros_message_to_rdf(message, rdf_content_type=content_type)
                except Exception as e:
                    logger.exception("Exception in serialization of %s RDF content type", content_type)
                    continue
                serialized_messages[webhook.content_type] = smsg
            webhook.notify(smsg)

    def register_webhook(self, topic, target_resource, callback_url, content_type):
        subscr = lrt_subscriptions.WebhookSubscription(
            target_resource=target_resource,
            callback_url=callback_url,
            callback=None,
            content_type=content_type,
            context={"topic": topic})
        subscr.register()
        self._webhooks[topic].append(subscr)
        self.subscribe(topic, sid=subscr.id)
        return subscr

    def register_websocket(self, topic, target_resource, websocket_url, content_type, notification_callback):
        subscr = lrt_subscriptions.WebsocketSubscription(
            target_resource=target_resource,
            websocket_url=websocket_url,
            callback=None,
            content_type=content_type,
            notification_callback=notification_callback,
            context={"topic": topic})
        subscr.register()
        self._webhooks[topic].append(subscr)
        self.subscribe(topic, sid=subscr.id)
        return subscr

    def unregister_webhook(self, subscr):
        topic = subscr.context.get("topic")
        self.unsubscribe(topic, sid=subscr.id)
        self._webhooks[topic].remove(subscr)
        subscr.unregister()

    def publish_ros_messages(self, topic, messages, latch=False, queue_size=100):
        # Register as a publishing client, propagating any exceptions
        publisher_manager.register(self.client_id, topic, latch=latch, queue_size=queue_size)
        self._published[topic] = True

        # Publish the message
        if isinstance(messages, (tuple, list)):
            for message in messages:
                publisher_manager.publish(self.client_id, topic, message, latch=latch, queue_size=queue_size)
        else:
            publisher_manager.publish(self.client_id, topic, messages, latch=latch, queue_size=queue_size)


class LRTWebSocket(WebSocketHandler):
    client_id_seed = None
    clients_connected = 0

    def initialize(self, path_prefix=None, resource_prefix=None):
        self.path_prefix = path_prefix
        self.resource_prefix = resource_prefix

    def prepare(self):
        # This is called before open and checks if topic is valid
        topic = self.path_kwargs.get("topic")

        topics = rosapi.proxy.get_topics(topics_glob)
        if topic not in topics:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic,))

        topic_type = rosapi.proxy.get_topic_type(topic, topics_glob)
        if not topic_type:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic,))

        http_accept = self.request.headers.get("Accept")
        accept_mimetypes = rdfutils.get_accept_mimetypes(http_accept)
        self.rdf_content_type = rdfutils.get_rdf_content_type(accept_mimetypes)

    def full_request_url(self):
        # returns same as self.request.full_url()
        # FIXME add support for additional X-* headers
        return self.request.protocol + "://" + self.request.host + self.request.path

    def full_root_url(self):
        return self.request.protocol + "://" + self.request.host + self.path_prefix

    def full_resource_url(self, path):
        return join_paths(self.request.protocol + "://" + self.request.host + self.resource_prefix, path)

    def open(self, topic):
        cls = self.__class__

        self.topic = topic

        this_url = self.full_request_url()

        try:
            self.state = LRTState(client_id=int(cls.client_id_seed.incr() - 1))
            self.set_nodelay(True)
            cls.clients_connected += 1
            self.state.register_websocket(self.topic,
                                          self.full_resource_url(slash_at_end("/topics" + self.topic)),
                                          this_url,
                                          self.rdf_content_type,
                                          notification_callback=self.send_message)
        except Exception as exc:
            rospy.logerr("Unable to accept incoming connection.  Reason: %s", str(exc))
        rospy.loginfo("RDF Client %d connected to topic %s.  %d clients total.", self.state.client_id, topic,
                      cls.clients_connected)

    def on_message(self, message):
        cls = self.__class__

    def on_close(self):
        cls = self.__class__
        cls.clients_connected -= 1
        self.state.destroy()
        rospy.loginfo("RDF Client disconnected. %d clients total.", cls.clients_connected)

    def send_message(self, message, binary=False):
        IOLoop.instance().add_callback(partial(self.write_message, message, binary=binary))

    def check_origin(self, origin):
        return True


class LinkedRoboticThing(tornado.web.RequestHandler):
    client_id_seed = 0
    state = LRTState()
    accepted_rdf_mimetypes = ", ".join(rdfutils.get_parseable_rdf_mimetypes())

    url_map = Map([
        Rule('/', endpoint='index'),
        Rule('/topics/', endpoint='get_all_topics', methods=["GET"]),
        Rule('/topics/', endpoint='advertise_topic', methods=["POST"]),
        Rule('/topics/<path:topic_name>/', endpoint='get_topic_by_name', methods=["GET"]),
        Rule('/topics/<path:topic_name>/', endpoint='post_topic_by_name', methods=["POST"]),
        Rule('/topics/<path:topic_name>/', endpoint='delete_topic_by_name', methods=["DELETE"]),
        Rule('/topics/<path:topic_name>/subscriptions/', endpoint='all_topic_subscriptions'),
        Rule('/topics/<path:topic_name>/subscriptions/<string:id>', endpoint='get_topic_subscription', methods=["GET"]),
        Rule('/topics/<path:topic_name>/subscriptions/<string:id>', endpoint='delete_topic_subscription',
             methods=["DELETE"])
    ])

    def initialize(self, client_id=None, path_prefix=None, websocket_prefix=None):
        cls = self.__class__
        if cls.state.client_id is None:
            if client_id is None:
                cls.state.client_id = int(cls.client_id_seed.incr() - 1)
            else:
                cls.state.client_id = client_id
        self.path_prefix = path_prefix
        self.websocket_prefix = websocket_prefix

        self.views = {
            'index': self.index,
            'get_topic_by_name': self.get_topic_by_name,
            'post_topic_by_name': self.post_topic_by_name,
            'delete_topic_by_name': self.delete_topic_by_name,
            'get_all_topics': self.get_all_topics,
            'advertise_topic': self.advertise_topic,
            'all_topic_subscriptions': self.all_topic_subscriptions,
            'get_topic_subscription': self.get_topic_subscription,
            'delete_topic_subscription': self.delete_topic_subscription
        }

        if not debug_switch.DEBUG_NO_FRAME_UPDATES:  # DEBUG 4 NO FRAME UPDATES
            pass
        # lrt_subscriptions.start_loop()

    def prepare(self):
        pass

    def slash_redirect(self):
        self.redirect(slash_at_end(self.full_request_url()))

    def full_request_url(self):
        # returns same as self.request.full_url()
        # FIXME add support for additional X-* headers
        return self.request.protocol + "://" + self.request.host + self.request.path

    def full_root_url(self):
        return self.request.protocol + "://" + self.request.host + self.path_prefix

    def full_websocket_url(self):
        return self.request.protocol + "://" + self.request.host + self.websocket_prefix

    def dispatch_request(self, path_info, method=None):
        if not path_info:
            # if path is empty or none redirect to root path
            self.slash_redirect()
            return

        urls = self.url_map.bind(server_name=self.request.host.lower(),
                                 script_name=self.path_prefix,
                                 url_scheme=self.request.protocol,
                                 default_method=self.request.method,
                                 query_args=self.request.query)

        try:
            endpoint, args = urls.match(path_info=path_info, method=method)
        except RequestRedirect as e:
            self.redirect(e.new_url, status=e.code)
            return
        except HTTPException as e:
            raise tornado.web.HTTPError(e.code, reason=e.description)
        self.views[endpoint](path_info=path_info, **args)

    # Tornado HTTP handlers

    def get(self, path):
        self.dispatch_request(path_info=path, method="GET")

    def post(self, path):
        self.dispatch_request(path_info=path, method="POST")

    def delete(self, path):
        self.dispatch_request(path_info=path, method="DELETE")

    # Logic Handlers

    def index(self, path_info=None):
        print('GET index', file=sys.stderr)
        self.get_graph()

    # Topics

    def add_topic_node_to_graph(self, graph, topic_path, topic_name):
        topic_type = proxy.get_topic_type(topic_name, topics_glob)
        if not topic_type:
            return None
        topic_node = rdflib.URIRef(topic_path)
        subscriptions_node = rdflib.URIRef(join_paths(topic_path, "subscriptions"))
        graph.add((topic_node, rdflib.RDF.type, ROS.Topic))
        graph.add((topic_node, ROS.type, rdflib.Literal(topic_type)))
        graph.add((topic_node, HYBRIT.subscriptions, subscriptions_node))
        graph.add((topic_node, HYBRIT.websocketUrl,
                   rdflib.Literal(join_paths(self.full_websocket_url(), topic_name), datatype=XSD.anyURI)))

        return topic_node

    def get_all_topics(self, path_info=None):
        print('GET get_all_topics')
        graph = hybrit_graph()

        this_url = self.full_request_url()

        this_resource = rdflib.URIRef(this_url)

        graph.add((this_resource, rdflib.RDF.type, LDP.BasicContainer))
        graph.add((this_resource, rdflib.RDF.type, LDP.Container))
        graph.add((this_resource, DCTERMS.title, rdflib.Literal("a list of all ROS topics")))

        topics = proxy.get_topics(topics_glob)
        for topic in topics:
            topic_node = self.add_topic_node_to_graph(graph, join_paths(this_resource, topic), topic)
            graph.add((this_resource, LDP.contains, topic_node))

        self.write_graph(graph, base=this_url)

    def advertise_topic(self, path_info=None):
        print('POST advertise_topic')

        topic_name = self.request.headers.get("Slug", None)
        if not topic_name:
            raise tornado.web.HTTPError(HTTP_BAD_REQUEST, reason="Topic name in Slug header is required")

        topic_name = slash_at_start(topic_name)

        this_url = self.full_request_url()
        topic_url = join_paths(this_url, topic_name)

        content_type = self.request.headers.get("Content-Type", "")
        rdf_format = rdfutils.get_parseable_rdf_format(content_type)
        if not rdf_format:
            raise tornado.web.HTTPError(HTTP_UNSUPPORTED_MEDIA_TYPE)
        try:
            rdf_graph = hybrit_graph().parse(data=self.request.body, publicID=topic_url, format=rdf_format)
            topic_node = rdflib.URIRef(topic_url)
        except Exception as e:
            msg = "Exception in deserialization of %s RDF content type" % rdf_format
            logging.exception(msg)
            raise tornado.web.HTTPError(HTTP_BAD_REQUEST, reason=msg)

        if (topic_node, rdflib.RDF.type, ROS.Topic) not in rdf_graph:
            raise tornado.web.HTTPError(HTTP_BAD_REQUEST, reason="Missing topic triple (%s, %s, %s)" % (
                topic_node, rdflib.RDF.type, ROS.Topic))

        msg_type, topic_data = rdfutils.ros_rdf_to_python(rdf_graph, topic_node)
        if not msg_type:
            raise tornado.web.HTTPError(HTTP_BAD_REQUEST, reason="Missing ROS message type of topic")

        try:
            self.state.advertise(topic_name, msg_type)
        except ros_loader.InvalidTypeStringException as e:
            raise tornado.web.HTTPError(HTTP_BAD_REQUEST, reason="Invalid ROS type %r: %s" % (msg_type, e))

        self.set_header("Location", topic_url)
        self.set_header("Link", '<http://www.w3.org/ns/ldp#Resource>; rel="type"')
        self.set_status(201)
        self.finish()

    def get_topic_by_name(self, path_info, topic_name):
        cls = self.__class__
        print('GET topic_by_name(%r)' % (topic_name,), file=sys.stderr)

        topic_name = slash_at_start(topic_name)

        this_url = self.full_request_url()

        topics = proxy.get_topics(topics_glob)
        if topic_name not in topics:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic_name,))

        # Handle Webhook Subscription
        if self.request.headers.get('Upgrade') in ('webhook', 'callback'):
            callback_url = self.request.headers.get('Callback')
            if not callback_url:
                raise tornado.web.HTTPError(HTTP_BAD_REQUEST, reasone='Missing callback header')

            topic_type = proxy.get_topic_type(topic_name, topics_glob)
            if not topic_type:
                raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic_name,))

            accept_mimetypes = rdfutils.get_accept_mimetypes(self.request.headers.get("Accept"))
            rdf_content_type = rdfutils.get_rdf_content_type(accept_mimetypes)

            subscr = cls.state.register_webhook(
                topic=topic_name,
                target_resource=this_url,
                callback_url=callback_url,
                content_type=rdf_content_type)

            graph = subscr.to_rdf()
            print("new callback " + this_url + " " + callback_url)
            self.write_graph(graph, base=this_url, rdf_content_type=rdf_content_type)
            return

        graph = hybrit_graph()

        this_resource = rdflib.URIRef(this_url)

        if not self.add_topic_node_to_graph(graph, this_resource, topic_name):
            raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic_name,))
        self.write_graph(graph, base=this_url)

    def post_topic_by_name(self, path_info, topic_name):
        cls = self.__class__
        print('POST post_topic_by_name(%r)' % (topic_name,), file=sys.stderr)
        topic_name = slash_at_start(topic_name)

        topics = proxy.get_topics(topics_glob)
        if topic_name not in topics:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic_name,))

        topic_type = proxy.get_topic_type(topic_name, topics_glob)
        if not topic_type:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic_name,))

        content_type = self.request.headers.get("Content-Type", "")
        rdf_format = rdfutils.get_parseable_rdf_format(content_type)
        self.set_header("Accept-Post", self.accepted_rdf_mimetypes)
        if not rdf_format:
            raise tornado.web.HTTPError(HTTP_UNSUPPORTED_MEDIA_TYPE)
        try:
            rdf_graph = hybrit_graph().parse(data=self.request.body, format=rdf_format)
            messages = rdfutils.extract_ros_messages(rdf_graph, add_ros_type_to_object=True)
        except Exception as e:
            msg = "Exception in deserialization of %s RDF content type" % rdf_format
            logging.exception(msg)
            raise tornado.web.HTTPError(HTTP_BAD_REQUEST, reason=msg)

        latch = False
        queue_size = 100

        cls.state.publish_ros_messages(topic_name, [message for _, message in messages], latch=latch,
                                       queue_size=queue_size)

    def delete_topic_by_name(self, path_info, topic_name):
        cls = self.__class__
        print('DELETE delete_topic_by_name(%r)' % (topic_name,), file=sys.stderr)
        topic_name = slash_at_start(topic_name)

        topics = proxy.get_topics(topics_glob)
        if topic_name not in topics:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic_name,))

        topic_type = proxy.get_topic_type(topic_name, topics_glob)
        if not topic_type:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND, reason="No such topic: %s" % (topic_name,))

        if not cls.state.unadvertise(topic_name):
            raise tornado.web.HTTPError(HTTP_BAD_REQUEST, reason="Topic %s was not advertised" % (topic_name,))

        self.set_header("Link", '<http://www.w3.org/ns/ldp#Resource>; rel="type"')
        self.set_status(204)
        self.finish()

    # Subscriptions

    def all_topic_subscriptions(self, path_info, topic_name):
        print('GET all_topic_subscriptions(%r)' % (topic_name,), file=sys.stderr)

        topic_name = slash_at_start(topic_name)

        def subscr_filter(subscr):
            try:
                return subscr.context.get("topic", None) == topic_name
            except ValueError:
                return False

        this_url = self.full_request_url()

        this_resource = rdflib.URIRef(this_url)

        graph = lrt_subscriptions.hybrit_graph()
        graph.add((this_resource, rdflib.RDF.type, LDP.BasicContainer))
        graph.add((this_resource, rdflib.RDF.type, LDP.Container))
        graph.add((this_resource, DCTERMS.title, rdflib.Literal("a list of subscriptions to topic %s" % (topic_name,))))

        for subscr in lrt_subscriptions.filter_subscriptions(filter_func=subscr_filter):
            graph.add((this_resource, lrt_subscriptions.HYBRIT.subscription, subscr.rdf_node()))
            subscr.to_rdf(graph=graph)

        self.write_graph(graph, base=this_url)

    def get_topic_subscription(self, path_info, topic_name, id):
        print('GET get_topic_subscription(%r,%r)' % (topic_name, id), file=sys.stderr)

        this_url = self.full_request_url()

        graph = lrt_subscriptions.hybrit_graph()

        subscr = lrt_subscriptions.get_subscription_by_id(id)
        if not subscr:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND)

        graph = subscr.to_rdf(graph=graph)
        self.write_graph(graph, base=this_url)

    def delete_topic_subscription(self, path_info, topic_name, id):
        cls = self.__class__
        print('DELETE delete_topic_subscription(%r,%r)' % (topic_name, id), file=sys.stderr)

        subscr = lrt_subscriptions.get_subscription_by_id(id)
        if not subscr:
            raise tornado.web.HTTPError(HTTP_NOT_FOUND)

        cls.state.unregister_webhook(subscr)

    # Utilities

    def write_graph(self, graph, base=None, rdf_content_type=None):
        if not rdf_content_type:
            http_accept = self.request.headers.get("Accept")
            accept_mimetypes = rdfutils.get_accept_mimetypes(http_accept)
            rdf_content_type = rdfutils.get_rdf_content_type(accept_mimetypes)

        content, content_type = rdfutils.serialize_rdf_graph(graph, content_type=rdf_content_type, base=base)
        self.set_header("Content-Type", content_type)
        self.write(content)
        self.finish()

    def get_graph(self, path=None):
        rdf_uri = UUID_URI
        if path:
            rdf_uri = join_paths(rdf_uri, path)

            reachable = rdfutils.get_reachable_statements(rdflib.URIRef(rdf_uri), LRT_GRAPH)
            if not reachable:
                raise tornado.web.HTTPError(HTTP_NOT_FOUND)
        else:
            reachable = LRT_GRAPH

        # self.request.full_url()
        # self.request.path
        root_path = copy_end_slash(self.request.path[:-len(path)] if path else self.request.path, self.request.path)
        url_root = slash_at_end(self.request.protocol + "://" + self.request.host + root_path)
        reachable = rdfutils.replace_uri_base(reachable, UUID_URI, url_root)

        is_empty = True

        graph = rdflib.Graph()
        graph.namespace_manager = LRT_GRAPH.namespace_manager
        for i in reachable:
            is_empty = False
            graph.add(i)

        if is_empty:
            raise tornado.web.HTTPError(404)

        self.write_graph(graph, base=url_root)

    def get_root_page(self):
        self.set_header("Content-type", "text/html")
        html = '''<html>
            <head><title>Robot RDF Server</title></head>
            <body>
            <h1>Robot RDF Server </h1>
            <div>
            <a href="robot"> Robot RDF </a>
            </div>
            </body>
            </html>'''
        self.write(html)
        self.finish()
