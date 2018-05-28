import threading


class AtomicInteger(object):
    def __init__(self, value=0):
        self._value = int(value)
        self._lock = threading.Lock()

    def incr(self, value=1):
        with self._lock:
            self._value += int(value)
            return self._value

    def __int__(self):
        with self._lock:
            return self._value

    def __add__(self, other):
        if isinstance(other, AtomicInteger):
            return AtomicInteger(self._value + other.value)
        with self._lock:
            return AtomicInteger(self._value + other)

    def __iadd__(self, other):
        self.incr(other)
        return self

    def __neg__(self):
        with self._lock:
            return AtomicInteger(-self._value)

    def __pos__(self):
        with self._lock:
            return AtomicInteger(self._value)

    def __sub__(self, other):
        if isinstance(other, AtomicInteger):
            return AtomicInteger(self._value - other.value)
        return AtomicInteger(self._value - other)

    def __isub__(self, other):
        self.incr(-other)
        return self

    def __hash__(self):
        raise TypeError("unhashable type: 'AtomicInteger'")

    def __eq__(self, other):
        with self._lock:
            if isinstance(other, AtomicInteger):
                return self._value == other.value
            return self._value == other

    def __ne__(self, other):
        with self._lock:
            if isinstance(other, AtomicInteger):
                return self._value != other.value
            return self._value != other

    def __str__(self):
        with self._lock:
            return str(self._value)

    def __repr__(self):
        return 'AtomicInteger(%d)' % self._value

    @property
    def value(self):
        with self._lock:
            return self._value

    @value.setter
    def value(self, value):
        with self._lock:
            self._value = int(value)
