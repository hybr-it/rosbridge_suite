import rospy
import six
import tornado
import tornado.web
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
from lrt_rdf import hybrit_graph, LDP, ROS

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
    hybrit:topics <topics> ;
    hybrit:subscriptions <topics/subscriptions> .


<topics>
    rdf:type ldp:BasicContainer ;
    rdf:type ldp:Container .

<topics/subscriptions>
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


def keep_end_slash(p1, p2):
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

    def initialize(self):
        self.rules = []
        self.add_rules((
            (r"^$", self.get_slash_redirect),
            (r"^/topics/subscriptions/ws/$", self.get_ws_test),
            (r"^/topics/subscriptions$", self.get_slash_redirect),
            (r"^/topics/subscriptions/$", self.get_all_subscriptions),
            (r"^/topics/subscriptions/(?P<name>.*)", self.get_subscription_by_name),
            (r"^/topics$", self.get_slash_redirect),
            (r"^/topics/$", self.get_all_topics),
            (r"^/topics(?P<topic>/.*)", self.get_topic_by_name),
        ))
        self.url_root = None
        if not debug_switch.DEBUG_NO_FRAME_UPDATES:  # DEBUG 4 NO FRAME UPDATES
            pass
        subscriptions.start_loop()

    def add_rules(self, rules):
        for pattern, func in rules:
            self.add_rule(pattern, func)

    def add_rule(self, pattern, func):
        self.rules.append((re.compile(pattern), func))

    def match_rule(self, path):
        for r, func in self.rules:
            mo = r.match(path)
            if mo:
                return mo, func
        return None, None

    def prepare(self):
        pass

    def get(self, path):
        # if path is None or path in ("/", ""):
        #    self.get_root_page()
        # else:
        if not path:
            path = ""
        mo, func = self.match_rule(path)
        if mo and func:
            func(path, mo)
        else:
            self.get_graph(path)

    def get_slash_redirect(self, path, mo):
        self.redirect(rdfutils.add_slash(self.request.protocol + "://" + self.request.host + self.request.path))

    # Topics

    def get_all_topics(self, path, mo):
        graph = hybrit_graph()

        request_path = keep_end_slash(self.request.path[:-len(path)] if path else self.request.path, self.request.path)
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

    def get_topic_by_name(self, path, mo):
        graph = hybrit_graph()

        request_path = keep_end_slash(self.request.path[:-len(path)] if path else self.request.path, self.request.path)
        this_url = self.request.protocol + "://" + self.request.host + request_path

        topic_name = mo.group("topic")

        this_resource = rdflib.URIRef(this_url)

        if not self.add_topic_node_to_graph(graph, this_resource, topic_name):
            raise tornado.web.HTTPError(404, reason="No such topic: %s" % (topic_name,))
        self.write_graph(graph, base=this_url)

    def add_topic_node_to_graph(self, graph, topic_path, topic_name):
        topic_type = proxy.get_topic_type(topic_name, topics_glob)
        if not topic_type:
            return None
        topic_node = rdflib.URIRef(topic_path)
        graph.add((topic_node, rdflib.RDF.type, ROS.Topic))
        graph.add((topic_node, ROS.type, rdflib.Literal(topic_type)))
        return topic_node

    # Subscriptions

    def get_ws_test(self, path, mo):
        return self.redirect(rdfutils.add_slash(self.request.protocol + "://" + self.request.host + "/rdf"))

    def get_all_subscriptions(self, path, mo):
        def subscr_filter(subscr):
            target_path = getattr(subscr, 'target_path')
            return target_path and target_path in ('/transforms', '/transforms/')

        request_path = keep_end_slash(self.request.path[:-len(path)] if path else self.request.path, self.request.path)
        this_url = self.request.protocol + "://" + self.request.host + request_path

        this_resource = rdflib.URIRef(this_url)

        graph = subscriptions.hybrit_graph()
        for subscr in subscriptions.filter_subscriptions(filter_func=subscr_filter):
            graph.add((this_resource, subscriptions.HYBRIT.subscription, subscr.rdf_node()))

        self.write_graph(graph, base=this_url)

    def get_subscription_by_name(self, path, mo):
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

    def get_graph(self, path):
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
        request_path = keep_end_slash(self.request.path[:-len(path)] if path else self.request.path, self.request.path)
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
