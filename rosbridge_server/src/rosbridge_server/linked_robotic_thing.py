from __future__ import print_function
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

UUID_URI = 'http://{}'.format(uuid.uuid4())


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


class LinkedRoboticThing(tornado.web.RequestHandler):

    def initialize(self, path_prefix=None):
        self.path_prefix = path_prefix
        self.url_map = Map([
            Rule('/', endpoint='index'),
            Rule('/topics/', endpoint='all_topics'),
            Rule('/topics/<path:topic_name>/', endpoint='topic_by_name'),
            Rule('/topics/<path:topic_name>/subscriptions/', endpoint='all_topic_subscriptions'),
            Rule('/topics/<path:topic_name>/subscriptions/<string:id>', endpoint='topic_subscription_by_id')
        ])

        self.views = {
            'index': self.index,
            'topic_by_name': self.topic_by_name,
            'all_topics': self.all_topics,
            'all_topic_subscriptions': self.all_topic_subscriptions,
            'topic_subscription_by_id': self.topic_subscription_by_id
        }
        if not debug_switch.DEBUG_NO_FRAME_UPDATES:  # DEBUG 4 NO FRAME UPDATES
            pass
        subscriptions.start_loop()

    def prepare(self):
        pass

    def slash_redirect(self):
        self.redirect(rdfutils.add_slash(self.request.protocol + "://" + self.request.host + self.request.path))

    def dispatch_request(self, path_info, method=None):
        if not path_info:
            # if path is empty or none redirect to root path
            self.slash_redirect()
            return

        wsgi_environ = tornado.wsgi.WSGIContainer.environ(self.request)
        wsgi_environ['SCRIPT_NAME'] = self.path_prefix
        urls = self.url_map.bind_to_environ(wsgi_environ)

        try:
            endpoint, args = urls.match(path_info=path_info, method=method)
        except RequestRedirect as e:
            self.redirect(e.new_url, status=e.code)
            return
        except HTTPException as e:
            raise tornado.web.HTTPError(e.code, reason=e.description)
        self.views[endpoint](path_info=path_info, **args)

    def get(self, path):
        self.dispatch_request(path_info=path, method="GET")

    def index(self, path_info=None):
        print('HIT index', file=sys.stderr)
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
        print('HIT all_topics')
        graph = hybrit_graph()

        request_path = copy_end_slash(self.request.path[:-len(path_info)] if path_info else self.request.path,
                                      self.request.path)
        this_url = self.request.protocol + "://" + self.request.host + request_path

        this_resource = rdflib.URIRef(this_url)

        graph.add((this_resource, rdflib.RDF.type, LDP.BasicContainer))
        graph.add((this_resource, rdflib.RDF.type, LDP.Container))
        graph.add((this_resource, DCTERMS.title, rdflib.Literal("a list of all ROS topics")))

        topics = proxy.get_topics(topics_glob)
        for topic in topics:
            topic_node = self.add_topic_node_to_graph(graph, join_paths(this_resource, topic), topic)
            graph.add((this_resource, LDP.contains, topic_node))

        self.write_graph(graph, base=this_url)

    def topic_by_name(self, path_info, topic_name):
        print('HIT topic_by_name(%r)' % (topic_name,), file=sys.stderr)
        graph = hybrit_graph()

        request_path = copy_end_slash(self.request.path[:-len(path_info)] if path_info else self.request.path,
                                      self.request.path)
        this_url = self.request.protocol + "://" + self.request.host + request_path

        if not topic_name.startswith('/'):
            topic_name = '/' + topic_name

        this_resource = rdflib.URIRef(this_url)

        if not self.add_topic_node_to_graph(graph, this_resource, topic_name):
            raise tornado.web.HTTPError(404, reason="No such topic: %s" % (topic_name,))
        self.write_graph(graph, base=this_url)


    # Subscriptions

    def all_topic_subscriptions(self, path_info, topic_name):
        print('HIT all_topic_subscriptions(%r)' % (topic_name,), file=sys.stderr)

        def subscr_filter(subscr):
            target_path = getattr(subscr, 'target_path')
            return target_path and target_path in ('/transforms', '/transforms/')

        request_path = copy_end_slash(self.request.path[:-len(path_info)] if path_info else self.request.path, self.request.path)
        this_url = self.request.protocol + "://" + self.request.host + request_path

        this_resource = rdflib.URIRef(this_url)

        graph = subscriptions.hybrit_graph()
        for subscr in subscriptions.filter_subscriptions(filter_func=subscr_filter):
            graph.add((this_resource, subscriptions.HYBRIT.subscription, subscr.rdf_node()))

        self.write_graph(graph, base=this_url)

    def topic_subscription_by_id(self, path_info, topic_name, id):
        print('HIT topic_subscription_by_id(%r,%r)' % (topic_name, id), file=sys.stderr)
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
        request_path = copy_end_slash(self.request.path[:-len(path)] if path else self.request.path, self.request.path)
        url_root = rdfutils.add_slash(self.request.protocol + "://" + self.request.host + request_path)
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
