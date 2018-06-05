import rdflib
from rdflib.namespace import DCTERMS

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
