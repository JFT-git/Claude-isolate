"""Host egress shared by the geolocation probe and the VM gateway."""
import ipaddress
import socket
import time


def public_address(value):
    """is_global alone includes multicast, which is not an Internet endpoint."""
    try:
        address = ipaddress.ip_address(value)
        return address.is_global and not address.is_multicast and not address.is_reserved
    except (ValueError, TypeError):
        return False


def open_public(host, port, timeout=3):
    # IPv4 only, consistently for both guest traffic and the IP check.
    # Resolve once, validate, then connect to that exact address: no second
    # DNS lookup that could turn a public hostname into a local destination.
    addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    if not addresses or any(not public_address(a[4][0]) for a in addresses):
        raise OSError('Destination resolves to a local/non-public address')
    deadline = time.monotonic() + timeout
    error = None
    for family, kind, protocol, _, address in addresses:
        s = socket.socket(family, kind, protocol)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Connection deadline expired')
            s.settimeout(remaining)
            s.connect(address)
            return s
        except OSError as e:
            error = e
            s.close()
    raise error or OSError('No reachable public address')
