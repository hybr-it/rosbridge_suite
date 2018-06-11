import rdflib
import rdflib.namespace
import rdflib.collection
import six
from werkzeug.datastructures import MIMEAccept
from werkzeug.http import parse_accept_header
import pygments
from pygments import highlight
from pygments.lexers.rdf import TurtleLexer
from html_formatter import HtmlFormatter
from pygments.filter import Filter
from pygments.token import Name

URI = Name.Variable.URI

# Special RDF type for highlighted HTML
RDF_HIGHLIGHTED_HTML = 'rdf-highlighted-html'

# Map RDF content type to RDF format name
RDF_MIMETYPES = {
    'text/turtle': 'turtle',
    'text/xml': 'xml',
    'application/rdf+xml': 'xml',
    'text/n3': 'n3',
    #    'application/trix': 'trix',      # Only for context-aware stores
    #    'application/n-quads': 'nquads', # Only for context-aware stores
    'application/n-triples': 'nt',
    'application/trig': 'trig',
    'text/plain': 'turtle',
    'text/html': RDF_HIGHLIGHTED_HTML
}

# used to filter out mimetypes that can't be parsed
NON_PARSEABLE_RDF_MIMETYPES = ["text/plain", "application/json"]

try:
    rdflib.plugin.get('json-ld', rdflib.serializer.Serializer)
    RDF_MIMETYPES['application/json'] = 'json-ld'
    RDF_MIMETYPES['application/ld+json'] = 'json-ld'
except rdflib.plugin.PluginException:
    pass


class LinkFilter(Filter):
    def __init__(self, **options):
        Filter.__init__(self, **options)

    def filter(self, lexer, stream):
        for ttype, value in stream:
            if ttype is pygments.token.Name.Variable and value.startswith('<') and value.endswith('>'):
                ttype = URI
                value = value[1:-1]
            yield ttype, value


class MyHtmlFormatter(HtmlFormatter):
    def format_parts(self, ttype, value, parts):
        if ttype is URI:
            parts[0] = "&lt;<a href=\"{}\">{}</a>&gt;".format(value, parts[0])
        return parts


def get_accept_mimetypes(accept_header):
    return parse_accept_header(accept_header, MIMEAccept)


def convert_rdf_to_html(rdf_graph, base=None, encoding=None, **args):
    rdf_turtle = rdf_graph.serialize(base=base, encoding=encoding, format='turtle', **args)
    # highlight
    formatter = MyHtmlFormatter(full=True)
    lexer = TurtleLexer()
    lexer.add_filter(LinkFilter())
    result = highlight(rdf_turtle, lexer, formatter)
    return result


def get_rdf_content_type(accept_mimetypes):
    if not isinstance(accept_mimetypes, MIMEAccept):
        accept_mimetypes = get_accept_mimetypes(accept_mimetypes)
    return accept_mimetypes.best_match(six.iterkeys(RDF_MIMETYPES))


def get_rdf_format(content_type):
    return RDF_MIMETYPES.get(content_type)


def get_parseable_rdf_format(content_type):
    if content_type not in NON_PARSEABLE_RDF_MIMETYPES:
        return RDF_MIMETYPES.get(content_type)
    else:
        return None


def get_parseable_rdf_mimetypes():
    return [k for k in RDF_MIMETYPES.keys() if k not in NON_PARSEABLE_RDF_MIMETYPES]


def serialize_rdf_graph(rdf_graph, accept_mimetypes=None, content_type=None,
                        destination=None, base=None, encoding=None, **args):
    """Returns tuple (serialized_rdf_str, rdf_content_type_str)"""
    if content_type is None:
        content_type = get_rdf_content_type(accept_mimetypes)
    rdf_format = get_rdf_format(content_type)
    if rdf_format and rdf_format != RDF_HIGHLIGHTED_HTML:
        result = rdf_graph.serialize(destination=destination,
                                     format=rdf_format,
                                     base=base,
                                     encoding=encoding,
                                     **args)
        return result, content_type

    # Output as highlighted HTML
    return convert_rdf_to_html(rdf_graph, base=base, encoding=encoding, **args), "text/html"


ROS = rdflib.Namespace("http://ros.org/#")
ROSBRIDGE = rdflib.Namespace("http://ros.org/rosbridge#")

ROS_TYPE_FIELD_NAME = "@rostype"


def create_jsonld_context(value, context_base=None):
    context = None
    if isinstance(value, dict):
        context = context_base.copy() if context_base is not None else {}
        for ikey, ivalue in six.iteritems(value):
            if ikey == ROS_TYPE_FIELD_NAME:
                id_value = "http://ros.org/#Type"
            else:
                id_value = "http://ros.org/#" + ikey
            key_ctx = {
                "@id": id_value,
            }
            if isinstance(ivalue, (tuple, list)):
                key_ctx["@container"] = "@list"
            context[ikey] = key_ctx
    return context


def _add_jsonld_context_to(value, top_context_base=None):
    context = None
    is_dict = isinstance(value, dict)
    if is_dict:
        context = value.get("@context")
        if context is not None:
            return context
        context = create_jsonld_context(value, context_base=top_context_base)
    # recursion
    if is_dict:
        for item in six.itervalues(value):
            _add_jsonld_context_to(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _add_jsonld_context_to(item)
    if is_dict and context is not None:
        value["@context"] = context
    return context


def add_jsonld_context_to_ros_message(value):
    context = _add_jsonld_context_to(value, {
        "ros": six.text_type(ROS),
        "rosbridge": six.text_type(ROSBRIDGE),
        "xsd": six.text_type(rdflib.namespace.XSD)
    })
    if context is not None:
        type = value.get("@type")
        if type is None:
            value["@type"] = "http://ros.org/#Message"
    return context


def add_jsonld_context_to_rosbridge_message(value):
    context = _add_jsonld_context_to(value, {
        "ros": six.text_type(ROS),
        "rosbridge": six.text_type(ROSBRIDGE),
        "xsd": six.text_type(rdflib.XSD)
    })
    if context is not None:
        op = value.get("op")
        if op is not None:
            value["@type"] = "http://ros.org/rosbridge#Message"
        msg = value.get("msg")
        if isinstance(msg, dict):
            add_jsonld_context_to_ros_message(msg)
    return context


def ros_rdf_to_python(graph, node, **kwargs):
    """Returns tuple (rostype, value)"""

    add_ros_type_to_object = bool(kwargs.get("add_ros_type_to_object", False))

    if isinstance(node, rdflib.Literal):
        return None, node.toPython()
    elif (node, rdflib.RDF.first, None) in graph and (node, rdflib.RDF.rest, None) in graph:
        cl = rdflib.collection.Collection(graph, node)
        return None, [ros_rdf_to_python(graph, i, **kwargs)[1] for i in cl]
    elif isinstance(node, rdflib.URIRef) or isinstance(node, rdflib.BNode):
        val = {}
        rostype = None
        for s, p, o in graph.triples((node, None, None)):
            if isinstance(p, rdflib.URIRef) and p.startswith(ROS):
                prefix, namespace, name = graph.compute_qname(p)
                member_type, member_value = ros_rdf_to_python(graph, o, **kwargs)
                if name == "Type":
                    rostype = member_value
                    if add_ros_type_to_object:
                        val[ROS_TYPE_FIELD_NAME] = member_value
                else:
                    val[name] = member_value
        return rostype, val
    return None, None


def extract_ros_messages(graph, **kwargs):
    messages = []
    for s, p, o in graph.triples((None, rdflib.RDF.type, ROS.Message)):
        messages.append(s)
    return [ros_rdf_to_python(graph, m, **kwargs) for m in messages]


def extract_rosbridge_messages(graph, **kwargs):
    messages = []
    for s, p, o in graph.triples((None, rdflib.RDF.type, ROSBRIDGE.Message)):
        messages.append(s)
    return [ros_rdf_to_python(graph, m, **kwargs) for m in messages]


def get_reachable_statements(node, graph, seen=None):
    """
    Computes fix point of all blank nodes reachable from passed node
    :return: list of statements
    """
    if seen is None:
        seen = {}
    if node in seen:
        return
    seen[node] = 1
    for stmt in graph.triples((node, None, None)):
        yield stmt
        obj = stmt[2]
        if isinstance(obj, rdflib.BNode):
            for i in get_reachable_statements(obj, graph, seen):
                yield i


def add_slash(s):
    return s if s.endswith('/') else s + '/'


def replace_uri_base(stmts, old_base, new_base):
    old_base = old_base.rstrip('/')
    new_base = new_base.rstrip('/')

    def replace_node(node):
        if isinstance(node, rdflib.URIRef):
            uri = node.toPython()
            if uri == old_base:
                node = rdflib.URIRef(new_base)
            elif uri.startswith(old_base) and uri[len(old_base)] == '/':
                node = rdflib.URIRef(new_base + uri[len(old_base):])
        return node

    for s, p, o in stmts:
        new_s = replace_node(s)
        new_p = replace_node(p)
        new_o = replace_node(o)
        yield (new_s, new_p, new_o)
