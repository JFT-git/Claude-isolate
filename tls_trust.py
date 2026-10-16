"""Verified host TLS, including Windows roots not yet populated by Schannel."""
from functools import lru_cache
import ssl
import sys


@lru_cache(maxsize=1)
def client_context():
    # Built once per process: loading the Windows stores and certifi's roots
    # took 0.24 s of CPU on a test laptop, and the IP check connects every
    # 2 s. Callers only wrap sockets with it; the context is never changed.
    context = ssl.create_default_context()
    if sys.platform == 'win32':
        # Windows populates some trusted roots lazily through Schannel. OpenSSL
        # cannot do that during its handshake, so the frozen worker also ships
        # Mozilla's maintained root set. Keep OS trust and hostname validation.
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
