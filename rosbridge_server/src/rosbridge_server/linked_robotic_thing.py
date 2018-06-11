from __future__ import print_function
import logging
import rospy
import six
import sys
import tornado
import tornado.web
import tornado.wsgi
import uuid
import rdflib
import rdflib.namespace
from rdflib.namespace import DCTERMS
from rosbridge_library.util import rdfutils
import re
import posixpath
from rosapi.glob_helper import get_globs, topics_glob, services_glob, params_glob
from rosapi import proxy, objectutils, params
import lrt_debug as debug
import lrt_debug_switch as debug_switch
import lrt_subscriptions as subscriptions
from lrt_rdf import hybrit_graph, LDP, ROS, HYBRIT
from werkzeug.routing import Map, Rule, HTTPException, NotFound, RequestRedirect
from rosbridge_library.internal.publishers import manager

UUID_URI = 'http://{}'.format(uuid.uuid4())

logger = logging.getLogger(__name__)


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


class LinkedRoboticThing(tornado.web.RequestHandler):
    client_id_seed = 0

    def initialize(self, client_id=None, path_prefix=None):
        cls = self.__class__
        if client_id is None:
            self.client_id = int(cls.client_id_seed.incr() - 1)
        else:
            self.client_id = client_id
        self.path_prefix = path_prefix
        self.url_map = Map([
            Rule('/', endpoint='index'),
            Rule('/topics/', endpoint='all_topics'),
            Rule('/topics/<path:topic_name>/', endpoint='get_topic_by_name', methods=["GET"]),
            Rule('/topics/<path:topic_name>/', endpoint='post_topic_by_name', methods=["POST"]),
            Rule('/topics/<path:topic_name>/subscriptions/', endpoint='all_topic_subscriptions'),
            Rule('/topics/<path:topic_name>/subscriptions/<string:id>', endpoint='topic_subscription_by_id')
        ])

        self.views = {
            'index': self.index,
            'get_topic_by_name': self.get_topic_by_name,
            'post_topic_by_name': self.post_topic_by_name,
            'all_topics': self.all_topics,
            'all_topic_subscriptions': self.all_topic_subscriptions,
            'topic_subscription_by_id': self.topic_subscription_by_id
        }
        self.accepted_rdf_mimetypes = ", ".join(rdfutils.get_parseable_rdf_mimetypes())

        self._on_destroy_called = False

        # Save the topics that are published on for the purposes of unregistering
        self._published = {}

        if not debug_switch.DEBUG_NO_FRAME_UPDATES:  # DEBUG 4 NO FRAME UPDATES
            pass
        subscriptions.start_loop()

    def __del__(self):
        self.destroy()

    def destroy(self):
        if not self._on_destroy_called:
            self._on_destroy_called = True
            self.on_destroy()

    def on_destroy(self):
        for topic in self._published:
            manager.unregister(self.client_id, topic)
        self._published.clear()

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
        return topic_node

    def all_topics(self, path_info=None):
        print('GET all_topics')
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

    def get_topic_by_name(self, path_info, topic_name):
        print('GET topic_by_name(%r)' % (topic_name,), file=sys.stderr)
        graph = hybrit_graph()

        this_url = self.full_request_url()

        topic_name = slash_at_start(topic_name)

        this_resource = rdflib.URIRef(this_url)

        if not self.add_topic_node_to_graph(graph, this_resource, topic_name):
            raise tornado.web.HTTPError(404, reason="No such topic: %s" % (topic_name,))
        self.write_graph(graph, base=this_url)

    def post_topic_by_name(self, path_info, topic_name):
        print('POST post_topic_by_name(%r)' % (topic_name,), file=sys.stderr)
        topic_name = slash_at_start(topic_name)

        topic_type = proxy.get_topic_type(topic_name, topics_glob)
        if not topic_type:
            raise tornado.web.HTTPError(404, reason="No such topic: %s" % (topic_name,))

        content_type = self.request.headers.get("Content-Type", "")
        rdf_format = rdfutils.get_parseable_rdf_format(content_type)
        self.set_header("Accept-Post", self.accepted_rdf_mimetypes)
        if not rdf_format:
            raise tornado.web.HTTPError(415)
        try:
            rdf_graph = hybrit_graph().parse(data=self.request.body, format=rdf_format)
            messages = rdfutils.extract_ros_messages(rdf_graph, add_ros_type_to_object=True)
            debug.log(messages)
        except Exception as e:
            msg = "Exception in deserialization of %s RDF content type" % rdf_format
            logging.exception(msg)
            raise tornado.web.HTTPError(400, reason=msg)
        latch = False
        queue_size = 100

        # Register as a publishing client, propagating any exceptions
        manager.register(self.client_id, topic_name, latch=latch, queue_size=queue_size)
        self._published[topic_name] = True

        # Publish the message
        for type, message in messages:
            manager.publish(self.client_id, topic_name, message, latch=latch, queue_size=queue_size)

    # Subscriptions

    def all_topic_subscriptions(self, path_info, topic_name):
        print('GET all_topic_subscriptions(%r)' % (topic_name,), file=sys.stderr)

        topic_name = slash_at_start(topic_name)

        def subscr_filter(subscr):
            target_path = getattr(subscr, 'target_path')
            return target_path and target_path in ('/transforms', '/transforms/')

        this_url = self.full_request_url()

        this_resource = rdflib.URIRef(this_url)

        graph = subscriptions.hybrit_graph()
        graph.add((this_resource, rdflib.RDF.type, LDP.BasicContainer))
        graph.add((this_resource, rdflib.RDF.type, LDP.Container))
        graph.add((this_resource, DCTERMS.title, rdflib.Literal("a list of subscriptions to topic %s" % (topic_name,))))

        for subscr in subscriptions.filter_subscriptions(filter_func=subscr_filter):
            graph.add((this_resource, subscriptions.HYBRIT.subscription, subscr.rdf_node()))

        self.write_graph(graph, base=this_url)

    def topic_subscription_by_id(self, path_info, topic_name, id):
        print('GET topic_subscription_by_id(%r,%r)' % (topic_name, id), file=sys.stderr)
        raise tornado.web.HTTPError(404)

    # Utilities

    def write_graph(self, graph, base=None):
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
                raise tornado.web.HTTPError(404)
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
