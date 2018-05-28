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
from rosbridge_library.protocol import Protocol
from rosbridge_library.capabilities.call_service import CallService
from rosbridge_library.capabilities.advertise import Advertise
from rosbridge_library.capabilities.publish import Publish
from rosbridge_library.capabilities.subscribe import Subscribe
# imports for defragmentation
from rosbridge_library.capabilities.defragmentation import Defragment
# imports for external service_server
from rosbridge_library.capabilities.advertise_service import AdvertiseService
from rosbridge_library.capabilities.service_response import ServiceResponse
from rosbridge_library.capabilities.unadvertise_service import UnadvertiseService

from rosbridge_library.protocol import has_binary
from rosbridge_library.util import json, bson

class RosbridgeRDFProtocol(Protocol):
    """ Adds the handlers for the rosbridge opcodes """
    rosbridge_capabilities = [CallService, Advertise, Publish, Subscribe,
                              Defragment, AdvertiseService, ServiceResponse, UnadvertiseService]

    print("registered capabilities (classes):")
    for cap in rosbridge_capabilities:
        print(" -", str(cap))

    parameters = None

    def __init__(self, client_id, parameters = None):
        self.parameters = parameters
        Protocol.__init__(self, client_id)
        for capability_class in self.rosbridge_capabilities:
            args = []
            kwargs = {}
            if isinstance(capability_class, (tuple, list)):
                capability_class_and_args = capability_class
                capability_class = capability_class_and_args[0]
                for x in capability_class_and_args[1:]:
                    if isinstance(x, (list, tuple)):
                        args.extend(x)
                    elif isinstance(x, dict):
                        kwargs.update(x)
            self.add_capability(capability_class, *args, **kwargs)

    def serialize(self, msg, cid=None):
        """ Turns a dictionary of values into the appropriate wire-level
        representation.

        Default behaviour uses JSON.  Override to use a different container.

        Keyword arguments:
        msg -- the dictionary of values to serialize
        cid -- (optional) an ID associated with this.  Will be logged on err.

        Returns a JSON string representing the dictionary
        """
        try:
            if has_binary(msg) or self.bson_only_mode:
                return bson.BSON.encode(msg)
            else:
                return json.dumps(msg)
        except:
            if cid is not None:
                # Only bother sending the log message if there's an id
                self.log("error", "Unable to serialize %s message to client"
                         % msg["op"], cid)
            return None

    def deserialize(self, msg, cid=None):

        """ Turns the wire-level representation into a dictionary of values

        Default behaviour assumes JSON. Override to use a different container.

        Keyword arguments:
        msg -- the wire-level message to deserialize
        cid -- (optional) an ID associated with this.  Is logged on error

        Returns a dictionary of values

        """
        try:
            if self.bson_only_mode:
                bson_message = bson.BSON(msg)
                return bson_message.decode()
            else:
                return json.loads(msg)
        except Exception as e:
            # if we did try to deserialize whole buffer .. first try to let self.incoming check for multiple/partial json-decodes before logging error
            # .. this means, if buffer is not == msg --> we tried to decode part of buffer

            # TODO: implement a way to have a final Exception when nothing works out to decode (multiple/broken/partial JSON..)

            # supressed logging of exception on json-decode to keep rosbridge-logs "clean", otherwise console logs would get spammed for every failed json-decode try
#            if msg != self.buffer:
#                error_msg = "Unable to deserialize message from client: %s"  % msg
#                error_msg += "\nException was: " +str(e)
#
#                self.log("error", error_msg, cid)

            # re-raise Exception to allow handling outside of deserialize function instead of returning None
            raise
            #return None
