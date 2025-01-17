#!/usr/bin/env python
# Software License Agreement (BSD License)
#
# Copyright (c) 2012, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import print_function
import rospy
import sys

from socket import error

from tornado.ioloop import IOLoop
from tornado.ioloop import PeriodicCallback
from tornado.web import Application

from rosbridge_server import RosbridgeWebSocket
from rosbridge_server import RosbridgeWebSocketRDF
from rosbridge_server import LinkedRoboticThing, LRTWebSocket

from rosbridge_library.capabilities.advertise import Advertise
from rosbridge_library.capabilities.publish import Publish
from rosbridge_library.capabilities.subscribe import Subscribe
from rosbridge_library.capabilities.advertise_service import AdvertiseService
from rosbridge_library.capabilities.unadvertise_service import UnadvertiseService
from rosbridge_library.capabilities.call_service import CallService
from rosbridge_library.util import AtomicInteger
import logging

def shutdown_hook():
    IOLoop.instance().stop()

if __name__ == "__main__":
    rospy.init_node("rosbridge_websocket")
    rospy.on_shutdown(shutdown_hook)    # register shutdown hook to stop the server

    # Added debug logging to console
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    # create formatter and add it to the handlers
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    # add the handlers to the logger
    logging.getLogger().addHandler(ch)

    ##################################################
    # Parameter handling                             #
    ##################################################
    retry_startup_delay = rospy.get_param('~retry_startup_delay', 2.0)  # seconds

    # get RosbridgeProtocol parameters
    RosbridgeWebSocket.fragment_timeout = rospy.get_param('~fragment_timeout',
                                                          RosbridgeWebSocket.fragment_timeout)
    RosbridgeWebSocketRDF.fragment_timeout = RosbridgeWebSocket.fragment_timeout
    RosbridgeWebSocket.delay_between_messages = rospy.get_param('~delay_between_messages',
                                                                RosbridgeWebSocket.delay_between_messages)
    RosbridgeWebSocketRDF.delay_between_messages = RosbridgeWebSocket.delay_between_messages
    RosbridgeWebSocket.max_message_size = rospy.get_param('~max_message_size',
                                                          RosbridgeWebSocket.max_message_size)
    RosbridgeWebSocketRDF.max_message_size = RosbridgeWebSocket.max_message_size
    RosbridgeWebSocket.unregister_timeout = rospy.get_param('~unregister_timeout',
                                                          RosbridgeWebSocket.unregister_timeout)
    RosbridgeWebSocketRDF.unregister_timeout = RosbridgeWebSocket.unregister_timeout
    bson_only_mode = rospy.get_param('~bson_only_mode', False)

    if RosbridgeWebSocket.max_message_size == "None":
        RosbridgeWebSocket.max_message_size = None
    if RosbridgeWebSocketRDF.max_message_size == "None":
        RosbridgeWebSocketRDF.max_message_size = None

    # SSL options
    certfile = rospy.get_param('~certfile', None)
    keyfile = rospy.get_param('~keyfile', None)
    # if authentication should be used
    RosbridgeWebSocket.authenticate = rospy.get_param('~authenticate', False)
    RosbridgeWebSocketRDF.authenticate = RosbridgeWebSocket.authenticate
    port = rospy.get_param('~port', 9090)
    address = rospy.get_param('~address', "")

    # Get the glob strings and parse them as arrays.
    RosbridgeWebSocket.topics_glob = [
        element.strip().strip("'")
        for element in rospy.get_param('~topics_glob', '')[1:-1].split(',')
        if len(element.strip().strip("'")) > 0]
    RosbridgeWebSocketRDF.topics_glob = RosbridgeWebSocket.topics_glob[:]
    RosbridgeWebSocket.services_glob = [
        element.strip().strip("'")
        for element in rospy.get_param('~services_glob', '')[1:-1].split(',')
        if len(element.strip().strip("'")) > 0]
    RosbridgeWebSocketRDF.services_glob = RosbridgeWebSocket.services_glob[:]
    RosbridgeWebSocket.params_glob = [
        element.strip().strip("'")
        for element in rospy.get_param('~params_glob', '')[1:-1].split(',')
        if len(element.strip().strip("'")) > 0]
    RosbridgeWebSocketRDF.params_glob = RosbridgeWebSocket.params_glob[:]

    if "--port" in sys.argv:
        idx = sys.argv.index("--port")+1
        if idx < len(sys.argv):
            port = int(sys.argv[idx])
        else:
            print("--port argument provided without a value.")
            sys.exit(-1)

    if "--address" in sys.argv:
        idx = sys.argv.index("--address")+1
        if idx < len(sys.argv):
            address = int(sys.argv[idx])
        else:
            print("--address argument provided without a value.")
            sys.exit(-1)

    if "--retry_startup_delay" in sys.argv:
        idx = sys.argv.index("--retry_startup_delay") + 1
        if idx < len(sys.argv):
            retry_startup_delay = int(sys.argv[idx])
        else:
            print("--retry_startup_delay argument provided without a value.")
            sys.exit(-1)

    if "--fragment_timeout" in sys.argv:
        idx = sys.argv.index("--fragment_timeout") + 1
        if idx < len(sys.argv):
            RosbridgeWebSocket.fragment_timeout = int(sys.argv[idx])
            RosbridgeWebSocketRDF.fragment_timeout = RosbridgeWebSocket.fragment_timeout
        else:
            print("--fragment_timeout argument provided without a value.")
            sys.exit(-1)

    if "--delay_between_messages" in sys.argv:
        idx = sys.argv.index("--delay_between_messages") + 1
        if idx < len(sys.argv):
            RosbridgeWebSocket.delay_between_messages = float(sys.argv[idx])
            RosbridgeWebSocketRDF.delay_between_messages = RosbridgeWebSocket.delay_between_messages
        else:
            print("--delay_between_messages argument provided without a value.")
            sys.exit(-1)

    if "--max_message_size" in sys.argv:
        idx = sys.argv.index("--max_message_size") + 1
        if idx < len(sys.argv):
            value = sys.argv[idx]
            if value == "None":
                RosbridgeWebSocket.max_message_size = None
            else:
                RosbridgeWebSocket.max_message_size = int(value)
            RosbridgeWebSocketRDF.max_message_size = RosbridgeWebSocket.max_message_size
        else:
            print("--max_message_size argument provided without a value. (can be None or <Integer>)")
            sys.exit(-1)

    if "--unregister_timeout" in sys.argv:
        idx = sys.argv.index("--unregister_timeout") + 1
        if idx < len(sys.argv):
            unregister_timeout = float(sys.argv[idx])
        else:
            print("--unregister_timeout argument provided without a value.")
            sys.exit(-1)

    if "--topics_glob" in sys.argv:
        idx = sys.argv.index("--topics_glob") + 1
        if idx < len(sys.argv):
            value = sys.argv[idx]
            if value == "None":
                RosbridgeWebSocket.topics_glob = []
            else:
                RosbridgeWebSocket.topics_glob = [element.strip().strip("'") for element in value[1:-1].split(',')]
            RosbridgeWebSocketRDF.topics_glob = RosbridgeWebSocket.topics_glob
        else:
            print("--topics_glob argument provided without a value. (can be None or a list)")
            sys.exit(-1)

    if "--services_glob" in sys.argv:
        idx = sys.argv.index("--services_glob") + 1
        if idx < len(sys.argv):
            value = sys.argv[idx]
            if value == "None":
                RosbridgeWebSocket.services_glob = []
            else:
                RosbridgeWebSocket.services_glob = [element.strip().strip("'") for element in value[1:-1].split(',')]
            RosbridgeWebSocketRDF.services_glob = RosbridgeWebSocket.services_glob
        else:
            print("--services_glob argument provided without a value. (can be None or a list)")
            sys.exit(-1)

    if "--params_glob" in sys.argv:
        idx = sys.argv.index("--params_glob") + 1
        if idx < len(sys.argv):
            value = sys.argv[idx]
            if value == "None":
                RosbridgeWebSocket.params_glob = []
            else:
                RosbridgeWebSocket.params_glob = [element.strip().strip("'") for element in value[1:-1].split(',')]
            RosbridgeWebSocketRDF.params_glob = RosbridgeWebSocket.params_glob
        else:
            print("--params_glob argument provided without a value. (can be None or a list)")
            sys.exit(-1)

    if ("--bson_only_mode" in sys.argv) or bson_only_mode:
        RosbridgeWebSocket.bson_only_mode = bson_only_mode
        RosbridgeWebSocketRDF.bson_only_mode = RosbridgeWebSocket.bson_only_mode

    # To be able to access the list of topics and services, you must be able to access the rosapi services.
    if RosbridgeWebSocket.services_glob:
        RosbridgeWebSocket.services_glob.append("/rosapi/*")
    if RosbridgeWebSocketRDF.services_glob:
        RosbridgeWebSocketRDF.services_glob.append("/rosapi/*")

    Subscribe.topics_glob = RosbridgeWebSocket.topics_glob
    Advertise.topics_glob = RosbridgeWebSocket.topics_glob
    Publish.topics_glob = RosbridgeWebSocket.topics_glob
    AdvertiseService.services_glob = RosbridgeWebSocket.services_glob
    UnadvertiseService.services_glob = RosbridgeWebSocket.services_glob
    CallService.services_glob = RosbridgeWebSocket.services_glob

    ##################################################
    # Done with parameter handling                   #
    ##################################################

    # Setup common client_id_seed generator
    client_id_seed = AtomicInteger()
    RosbridgeWebSocket.client_id_seed = client_id_seed
    RosbridgeWebSocketRDF.client_id_seed = client_id_seed
    LinkedRoboticThing.client_id_seed = client_id_seed
    LRTWebSocket.client_id_seed = client_id_seed

    application = Application([(r"/", RosbridgeWebSocket), (r"", RosbridgeWebSocket),
                               (r"/rdf/", RosbridgeWebSocketRDF), (r"/rdf", RosbridgeWebSocketRDF),
                               (r"/lrt(?P<path>/.*)?", LinkedRoboticThing, dict(path_prefix="/lrt", websocket_prefix="/lrtws")),
                               (r"/lrtws/topics(?P<topic>/.*)?", LRTWebSocket, dict(path_prefix="/lrtws/topics", resource_prefix="/lrt"))
                               ])

    connected = False
    while not connected and not rospy.is_shutdown():
        try:
            if certfile is not None and keyfile is not None:
                application.listen(port, address, ssl_options={ "certfile": certfile, "keyfile": keyfile}, xheaders=True)
            else:
                application.listen(port, address, xheaders=True)
            rospy.loginfo("Rosbridge WebSocket server started on port %d", port)
            rospy.loginfo("Modified version for HybrIT project")
            connected = True
        except error as e:
            rospy.logwarn("Unable to start server: " + str(e) +
                          " Retrying in " + str(retry_startup_delay) + "s.")
            rospy.sleep(retry_startup_delay)

    IOLoop.instance().start()
