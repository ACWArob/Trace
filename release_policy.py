"""Small, bounded controls for a single-process friends beta."""
from collections import OrderedDict
import ipaddress
import os
import threading
import time


def production_mode():
    return os.environ.get('TRACE_ENV', '').strip() == 'production'


def signup_mode():
    mode = os.environ.get('TRACE_SIGNUP_MODE', '').strip() or ('invite' if production_mode() else 'open')
    if mode not in ('open', 'invite', 'closed'):
        raise ValueError('TRACE_SIGNUP_MODE must be open, invite, or closed.')
    return mode


def client_ip(peer, headers):
    """The rate-limit key for a request.

    Only a configured proxy may supply the client address: the loopback Caddy
    proxy (``TRACE_TRUST_PROXY=1``, header ``X-Trace-Client-IP``) or Vercel's
    edge (``TRACE_TRUST_PROXY=vercel``, first entry of ``x-vercel-forwarded-for``,
    which Vercel sets itself).  ``X-Real-IP`` and ``X-Forwarded-For`` are never
    read.  IPv6 addresses are grouped by /64.
    """
    trust = os.environ.get('TRACE_TRUST_PROXY') if production_mode() else None
    address = None
    # Off Vercel the header is client-controlled, so only trust it on Vercel.
    if trust == 'vercel' and os.environ.get('VERCEL') == '1':
        forwarded = headers.get('X-Vercel-Forwarded-For', '') or ''
        try:
            address = ipaddress.ip_address(forwarded.split(',', 1)[0].strip())
        except ValueError:
            pass
    if address is None:
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return 'unknown'
        if trust == '1' and address.is_loopback:
            forwarded = headers.get('X-Trace-Client-IP', '')
            try:
                address = ipaddress.ip_address(forwarded)
            except ValueError:
                pass
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


class RequestBudget:
    def __init__(self, maximum_keys=5000, clock=time.monotonic):
        self.buckets = OrderedDict()
        self.maximum_keys = maximum_keys
        self.clock = clock
        self.lock = threading.Lock()

    def allow(self, key, limit, seconds=60):
        now = self.clock()
        with self.lock:
            values = [stamp for stamp in self.buckets.pop(key, []) if now - stamp < seconds]
            allowed = len(values) < limit
            if allowed:
                values.append(now)
            self.buckets[key] = values
            while len(self.buckets) > self.maximum_keys:
                self.buckets.popitem(last=False)
            return allowed


def library_byte_limit():
    value = int(os.environ.get('TRACE_LIBRARY_BYTES', str(100 * 1024 * 1024)))
    if not 1_500_000 <= value <= 1024 * 1024 * 1024:
        raise ValueError('TRACE_LIBRARY_BYTES must be between 1500000 and 1073741824.')
    return value
