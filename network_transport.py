"""Host egress shared by the geolocation probe and the VM gateway."""
import ipaddress
import http.client
import json
import re
import socket
import ssl
import time
from urllib.parse import urlencode


FAKE_IP_RANGE = ipaddress.ip_network('198.18.0.0/15')
DOH_HOST = 'cloudflare-dns.com'
DOH_BOOTSTRAP = ('1.1.1.1', '1.0.0.1')


def public_address(value):
    """is_global alone includes multicast, which is not an Internet endpoint."""
    try:
        address = ipaddress.ip_address(value)
        return address.is_global and not address.is_multicast and not address.is_reserved
    except (ValueError, TypeError):
        return False


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Истекло время проверки подключения')
    return remaining


def _connect(addresses, deadline):
    """Connect to already validated IPv4 addresses, without another lookup."""
    error = None
    for family, kind, protocol, _, address in addresses:
        s = socket.socket(family, kind, protocol)
        try:
            s.settimeout(_remaining(deadline))
            s.connect(address)
            return s
        except OSError as e:
            error = e
            s.close()
    raise error or OSError('No reachable public address')


def _public_hostname(host):
    # A fake-IP literal must never become an accepted destination. Only a
    # public-looking DNS name may be resolved using the fallback resolver.
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return (len(host) <= 253 and '.' in host
                and not host.lower().endswith(('.local', '.localhost', '.internal', '.lan'))
                and all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                        for label in host.split('.')))


def _doh_addresses(host, port, deadline):
    """Replace synthetic VPN DNS answers with real, validated public A records.

    HTTPS follows the same host routing/VPN as other outbound connections.
    Bootstrap addresses are public constants; certificate verification and
    TLS SNI authenticate the resolver. No local DNS, redirects or recursion.
    The synthetic address is NEVER used for a connection.
    """
    conn = http.client.HTTPConnection(DOH_HOST, 443, timeout=_remaining(deadline))
    try:
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))
                     for ip in DOH_BOOTSTRAP]
        raw = _connect(addresses, deadline)
        try:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            raw.settimeout(_remaining(deadline))
            conn.sock = context.wrap_socket(raw, server_hostname=DOH_HOST)
        except BaseException:
            raw.close()
            raise
        conn.sock.settimeout(_remaining(deadline))
        conn.request('GET', '/dns-query?' + urlencode({'name': host, 'type': 'A'}),
                     headers={'Accept': 'application/dns-json', 'Connection': 'close'})
        conn.sock.settimeout(_remaining(deadline))
        response = conn.getresponse()
        if response.status != 200:
            raise OSError(f'DNS-over-HTTPS: HTTP {response.status}')
        payload = response.read(65537)
        _remaining(deadline)
        if len(payload) > 65536:
            raise ValueError('DNS reply too large')
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get('Status') != 0:
            raise ValueError('DNS name not resolved')
        records = data.get('Answer')
        if not isinstance(records, list) or not 1 <= len(records) <= 64:
            raise ValueError('No DNS answers')
        ips = [record['data'] for record in records if record['type'] == 1]
        if not ips or any(not isinstance(ip, str) or not public_address(ip)
                          or ipaddress.ip_address(ip).version != 4 for ip in ips):
            raise ValueError('DNS reply contains non-public addresses')
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port))
                for ip in dict.fromkeys(ips)]
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
        raise OSError(f'VPN вернул виртуальный DNS-адрес для {host}, но безопасное '
                      f'разрешение имени не удалось: {error}. Проверьте VPN в режиме TUN '
                      'и доступ к DNS-over-HTTPS.') from error
    finally:
        conn.close()


def open_public(host, port, timeout=3):
    # IPv4 only, consistently for both guest traffic and the IP check.
    # Connect to the exact validated address; never allow local DNS rebinding.
    deadline = time.monotonic() + timeout
    addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    if not addresses:
        raise OSError(f'DNS не вернул адрес для {host}')
    if any(not public_address(a[4][0]) for a in addresses):
        if (_public_hostname(host)
                and all(ipaddress.ip_address(a[4][0]) in FAKE_IP_RANGE for a in addresses)):
            addresses = _doh_addresses(host, port, deadline)
        else:
            rejected = ', '.join(dict.fromkeys(a[4][0] for a in addresses))[:256]
            raise OSError(f'DNS для {host} вернул локальный или непубличный адрес '
                          f'({rejected}). Доступ закрыт для защиты компьютера; '
                          'проверьте DNS и режим TUN вашего VPN.')
    return _connect(addresses, deadline)
