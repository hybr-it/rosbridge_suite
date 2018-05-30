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


UUID_URI = 'http://{}'.format(uuid.uuid4())

HYBRIT = rdflib.Namespace('https://hybr-it-projekt.de/ns/hybr-it#')
SUBSCRIPTION = rdflib.Namespace('https://hybr-it-projekt.de/ns/subscription#')
VOM = rdflib.Namespace('http://vocab.arvida.de/2015/06/vom/vocab#')
MATHS = rdflib.Namespace('http://vocab.arvida.de/2015/06/maths/vocab#')
SPATIAL = rdflib.Namespace('http://vocab.arvida.de/2015/06/spatial/vocab#')
LDP = rdflib.Namespace('http://www.w3.org/ns/ldp#')
ROS = rdflib.Namespace("http://ros.org/#")


def add_namespaces(graph):
    namespace_manager = rdflib.namespace.NamespaceManager(graph)
    namespace_manager.bind('hybrit', HYBRIT, override=False)
    namespace_manager.bind('vom', VOM, override=False)
    namespace_manager.bind('maths', MATHS, override=False)
    namespace_manager.bind('spatial', SPATIAL, override=False)
    namespace_manager.bind('ldp', LDP, override=False)
    namespace_manager.bind('ros', ROS, override=False)
    return graph


def hybrit_graph(graph=None):
    if graph is None:
        graph = rdflib.Graph()
    namespace_manager = graph.namespace_manager
    namespace_manager.bind('hybrit', HYBRIT, override=False)
    namespace_manager.bind('subscription', SUBSCRIPTION, override=False)
    namespace_manager.bind('vom', VOM, override=False)
    namespace_manager.bind('maths', MATHS, override=False)
    namespace_manager.bind('spatial', SPATIAL, override=False)
    namespace_manager.bind('ldp', LDP, override=False)
    namespace_manager.bind('ros', ROS, override=False)
    namespace_manager.bind('dcterms', DCTERMS, override=False)
    return graph


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
    rdf:type hybrit:RoboticThing;
    hybrit:topics <topics> .

<topics>
    rdf:type ldp:BasicContainer .
    """.format(root=UUID_URI)
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


class LinkedRoboticThing(tornado.web.RequestHandler):

    def initialize(self):
        self.rules = []
        self.add_rules({
            r"/topics(?P<topic>/.*)?": self.get_topics
        })

    def add_rules(self, rules):
        for pattern, func in six.iteritems(rules):
            self.add_rule(pattern, func)

    def add_rule(self, pattern, func):
        self.rules.append((re.compile(pattern), func))

    def match_rule(self, path):
        for r, func in self.rules:
            mo = r.match(path)
            if mo:
                return mo, func
        return None, None

    def get(self, path):
        #if path is None or path in ("/", ""):
        #    self.get_root_page()
        #else:
        if not path:
            path = ""
        mo, func = self.match_rule(path)
        if mo and func:
            func(path, mo)
        else:
            self.get_graph(path)

    def get_topics(self, path, mo):

        graph = hybrit_graph()

        url_root = self.request.path[:-len(path)] if path else self.request.path

        topic_name = mo.group("topic")

        root_node = rdflib.URIRef(join_paths(url_root, path))

        if topic_name:
            self.add_topic_node_to_graph(graph, root_node, topic_name)
        else:
            graph.add((root_node, rdflib.RDF.type, LDP.BasicContainer))
            graph.add((root_node, DCTERMS.title, rdflib.Literal("a list of all topics")))

            topics = proxy.get_topics(topics_glob)
            for topic in topics:
                topic_node = self.add_topic_node_to_graph(graph, join_paths(root_node, topic), topic)
                graph.add((root_node, LDP.contains, topic_node))

        http_accept = self.request.headers.get("Accept")
        accept_mimetypes = rdfutils.get_accept_mimetypes(http_accept)
        rdf_content_type = rdfutils.get_rdf_content_type(accept_mimetypes)
        content, content_type = rdfutils.serialize_rdf_graph(graph, content_type=rdf_content_type)
        self.set_header("Content-Type", content_type)
        self.write(content)
        self.finish()

    def add_topic_node_to_graph(self, graph, topic_path, topic_name):
        topic_type = proxy.get_topic_type(topic_name, topics_glob)
        topic_node = rdflib.URIRef(topic_path)
        graph.add((topic_node, rdflib.RDF.type, ROS.Topic))
        graph.add((topic_node, ROS.type, rdflib.Literal(topic_type)))
        return topic_node

    def get_graph(self, path):
        rdf_uri = UUID_URI
        if path:
            if path == "/":
                # root path is mapped to UUID_URI
                pass
            else:
                if not path.startswith('/'):
                    rdf_uri += '/'
                rdf_uri += path

            reachable = rdfutils.get_reachable_statements(rdflib.URIRef(rdf_uri), LRT_GRAPH)
            if not reachable:
                raise tornado.web.HTTPError(404)
        else:
            reachable = LRT_GRAPH

        http_accept = self.request.headers.get("Accept")
        accept_mimetypes = rdfutils.get_accept_mimetypes(http_accept)
        rdf_content_type = rdfutils.get_rdf_content_type(accept_mimetypes)

        # self.request.full_url()
        # self.request.path
        # self.protocol + "://" + self.request.host + self.request.path
        url_root = self.request.path[:-len(path)] if path else self.request.path  # FIXME
        reachable = rdfutils.replace_uri_base(reachable, UUID_URI, url_root)

        is_empty = True

        g = rdflib.Graph()
        g.namespace_manager = LRT_GRAPH.namespace_manager
        for i in reachable:
            is_empty = False
            g.add(i)

        if is_empty:
            raise tornado.web.HTTPError(404)

        content, content_type = rdfutils.serialize_rdf_graph(g, content_type=rdf_content_type)
        self.set_header("Content-Type", content_type)
        self.write(content)
        self.finish()

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
