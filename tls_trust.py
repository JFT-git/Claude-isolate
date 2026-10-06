"""Verified host TLS, including Windows roots not yet populated by Schannel."""
import ssl
import sys


def client_context():
    context = ssl.create_default_context()
    if sys.platform == 'win32':
        # Windows populates some trusted roots lazily through Schannel. OpenSSL
        # cannot do that during its handshake, so the frozen worker also ships
        # Mozilla's maintained root set. Keep OS trust and hostname validation.
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
