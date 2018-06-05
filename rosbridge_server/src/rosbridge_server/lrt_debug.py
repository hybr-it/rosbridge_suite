from __future__ import print_function
import sys
import threading

LOG_LOCK = threading.Lock()
_ENABLED = False


def enable(flag=True):
    global _ENABLED
    _ENABLED = flag


def log(*args, **kwargs):
    global _ENABLED
    if _ENABLED:
        if 'file' not in kwargs:
            nkwargs = dict(kwargs, file=sys.stderr)
        else:
            nkwargs = kwargs
        with LOG_LOCK:
            print(*args, **nkwargs)
