"""A host's share of one project-wide HTTP allowance, separate from thread count."""
import threading


def split_budget(total, rank, hosts):
    if (not all(type(v) is int for v in (total,rank,hosts)) or not 1 <= total <= 40 or hosts < 1 or not 0 <= rank < hosts):
        raise ValueError('HTTP budget must be 1..40 and host rank must exist')
    return total // hosts + int(rank < total % hosts)


class Admission:
    def __init__(self, limit):
        if type(limit) is not int or not 1 <= limit <= 40:
            raise ValueError('HTTP admission must be 1..40')
        self.limit = limit
        self.slots = threading.BoundedSemaphore(limit)
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def __enter__(self):
        self.slots.acquire()
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        return self

    def __exit__(self, *error):
        with self.lock:
            self.active -= 1
        self.slots.release()


class LimitedTransport:
    def __init__(self, transport, admission):
        self.transport, self.admission = transport, admission

    def request(self, *args, **kwargs):
        with self.admission:
            return self.transport.request(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.transport, name)
