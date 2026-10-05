"""Fail-closed egress geolocation lease. Does not prove per-domain routing."""
import http.client
from functools import lru_cache
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import time
import threading
from network_transport import open_public, public_address

LEASE_SECONDS = 6
CHECK_INTERVAL = 2
RELAY_INTERVAL = .02
COUNTRIES = set('AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW'.split())


def classify(data):
    if not isinstance(data, dict):
        return {'allowed': False, 'reason': 'Invalid geolocation response'}
    country = data.get('country_code', data.get('country'))
    try:
        addr = ipaddress.ip_address(data.get('ip', ''))
    except (ValueError, TypeError):
        return {'allowed': False, 'reason': 'Missing or invalid external IP'}
    if not public_address(addr) or not isinstance(country, str) or country not in COUNTRIES:
        return {'allowed': False, 'reason': 'Unknown country or non-public IP'}
    return {'allowed': country != 'RU', 'ip': str(addr), 'country': country,
            'reason': 'Russian exit IP' if country == 'RU' else 'Non-Russian exit reported'}


class ProbeUnavailable(RuntimeError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def classify_trace(body):
    # loc is the source IP country; colo is a data-centre code, not a country.
    fields = {}
    for line in body.decode('ascii').splitlines():
        key, separator, value = line.partition('=')
        if separator and key in ('ip', 'loc'):
            if key in fields:
                return {'allowed': False, 'reason': 'Ambiguous IP check response'}
            fields[key] = value
    return classify({'ip': fields.get('ip'), 'country': fields.get('loc')})


def probe(port=None, mode='proxy'):
    token = os.environ.get('IPINFO_TOKEN', '')
    if token and not re.fullmatch(r'[A-Za-z0-9_-]+', token):
        raise ValueError('Invalid IPINFO_TOKEN format')
    # The unauthenticated IPinfo API's quota is too small for a live guard.
    # Cloudflare's diagnostic endpoint reports both IP and country per request.
    host, path = ('api.ipinfo.io', '/lite/me') if token else ('www.cloudflare.com', '/cdn-cgi/trace')
    if mode not in ('system', 'proxy'):
        raise ValueError('Unsupported network mode')
    conn = http.client.HTTPConnection('127.0.0.1' if mode == 'proxy' else host,
                                     port if mode == 'proxy' else 443, timeout=3)
    try:
        if mode == 'proxy':
            conn.set_tunnel(host, 443)
            conn.connect()
        else:
            conn.sock = open_public(host, 443, timeout=3)
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        conn.sock = context.wrap_socket(conn.sock, server_hostname=host)
        headers = {'Accept': 'application/json' if token else 'text/plain',
                   'Cache-Control': 'no-cache', 'Connection': 'close',
                   'User-Agent': 'IsolatedDesktop-NetworkGuard/0.1'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        conn.request('GET', path, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            if response.status == 429:
                retry = response.getheader('Retry-After', '60')
                retry = min(300, max(15, int(retry))) if str(retry).isdigit() else 60
                raise ProbeUnavailable('Сервис проверки IP ограничил запросы (429). Сеть закрыта; повторная проверка позже.', retry)
            raise ProbeUnavailable(f'Сервис проверки IP недоступен (HTTP {response.status}).')
        body = response.read(8193)
        if len(body) > 8192:
            raise RuntimeError('Oversized geolocation response')
        return classify(json.loads(body)) if token else classify_trace(body)
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _windows_file_api():
    import ctypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    api.CreateFileW.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    api.SetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                               ctypes.c_void_p, ctypes.c_uint32]
    api.SetFileInformationByHandle.restype = ctypes.c_int
    return api


def _state_stream(path):
    """Windows readers must allow atomic replacement while their handle is open."""
    if os.name != 'nt':
        return Path(path).open('r', encoding='utf-8')
    import ctypes
    import msvcrt
    api = _windows_file_api()
    # GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING, NORMAL.
    handle = api.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    return os.fdopen(fd, 'r', encoding='utf-8')


def _replace_state(temporary, path):
    if os.name != 'nt':
        temporary.replace(path)
        return
    import ctypes
    api = _windows_file_api()
    encoded = str(path.absolute()).encode('utf-16-le')
    class RenameInfo(ctypes.Structure):
        _fields_ = [('Flags', ctypes.c_uint32), ('RootDirectory', ctypes.c_void_p),
                    ('FileNameLength', ctypes.c_uint32), ('FileName', ctypes.c_ubyte * (len(encoded) + 2))]
    info = RenameInfo()
    # FileRenameInfoEx=22, REPLACE_IF_EXISTS|POSIX_SEMANTICS. MoveFileExW
    # (os.replace) cannot replace a held target even with FILE_SHARE_DELETE.
    # https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information
    info.Flags = 3
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(info) + RenameInfo.FileName.offset, encoded, len(encoded))
    handle = api.CreateFileW(str(temporary), 0x10000, 7, None, 3, 0x80, None)  # DELETE access.
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not api.SetFileInformationByHandle(handle, 22, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)


def read_state(path):
    with _state_stream(path) as stream:
        value = stream.read(65537)
    if len(value) > 65536:
        raise ValueError('Oversized network status')
    return json.loads(value)


def publish(path, result):
    value = dict(result, expires=time.monotonic() + LEASE_SECONDS if result.get('allowed') else 0,
                 wall_expires=time.time() + LEASE_SECONDS if result.get('allowed') else 0)
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.' + path.name + '-', suffix='.tmp', delete=False) as output:
        temporary = Path(output.name)
        json.dump(value, output)
    try:
        for attempt in range(20):
            try:
                _replace_state(temporary, path)
                break
            except PermissionError as error:
                # Other host software may temporarily deny delete sharing too.
                if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 19:
                    raise
                time.sleep(.01)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # A host scanner can keep the abandoned temporary file open.


def permitted(path, now=None):
    if not path:
        return False
    if Path(path).with_suffix('.revoked').exists() or Path(path).with_suffix('.paused').exists():
        return False
    external = os.environ.get('CLAUDE_NETWORK_REVOKE')
    if external and Path(external).exists():
        return False
    try:
        data = read_state(path)
        if not isinstance(data, dict) or not classify(data).get('allowed'):
            return False
        expiry = data['expires']
        return (data.get('allowed') is True and data.get('country') in COUNTRIES
                and data['country'] != 'RU' and type(expiry) in (float, int)
                and 0 < expiry - (time.monotonic() if now is None else now) <= LEASE_SECONDS
                and 0 < data.get('wall_expires', 0) - time.time() <= LEASE_SECONDS)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def revoke(lease, status_path=None, reason='Сеть закрыта до перезапуска среды.'):
    # One-way marker wins over any concurrent successful geolocation response.
    for path in (lease, status_path):
        if path:
            marker = Path(path).with_suffix('.revoked')
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            if fd is not None:
                with os.fdopen(fd, 'w', encoding='utf-8') as output:
                    output.write(reason)


class SessionPolicy:
    """Pin the first approved address; never silently unlock a session."""
    def __init__(self, initial):
        verified = classify(initial)
        if not verified.get('allowed'):
            raise ValueError('An approved public IP is required')
        self.ip = verified['ip']
        self.blocked = None

    def evaluate(self, result):
        if self.blocked is not None:
            return dict(self.blocked)
        current = classify(result) if result.get('allowed') else dict(result)
        if not current.get('allowed'):
            self.blocked = dict(current, allowed=False, locked=True,
                                reason=('Обнаружен российский IP. Включите VPN и перезапустите среду.'
                                        if current.get('country') == 'RU' else
                                        'Проверка сети не пройдена. Нужен перезапуск среды.'))
        elif current['ip'] != self.ip:
            self.blocked = dict(current, allowed=False, locked=True,
                                reason='IP изменился. Сеть закрыта до перезапуска среды.')
        return dict(self.blocked if self.blocked is not None else current,
                    pinned_ip=self.ip)


class NetworkEvents:
    """Pause immediately; only a check begun AFTER the latest event may reopen."""
    def __init__(self, lease, status_path=None):
        self.paths = [Path(p) for p in (lease, status_path) if p]
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.generation = 0

    def pause(self, reason='Проверяю подключение после сетевого события…'):
        with self.lock:
            self.generation += 1
            for path in self.paths:
                path.with_suffix('.paused').write_text(reason, encoding='utf-8')
            self.wake.set()

    def snapshot(self):
        with self.lock:
            self.wake.clear()
            return self.generation

    def verified(self, generation, result):
        with self.lock:
            if generation != self.generation:
                return False
            for path in self.paths:
                publish(path, result)
            for path in self.paths:
                path.with_suffix('.paused').unlink(missing_ok=True)
            return True


def monitor(port, lease, stop, mode='proxy', initial=None, status_path=None, events=None):
    if initial is None:
        initial = read_state(lease)
    policy = SessionPolicy(initial)
    events = events or NetworkEvents(lease, status_path)
    while not stop.is_set():
        events.wake.wait(CHECK_INTERVAL)
        if stop.is_set():
            return
        if Path(lease).with_suffix('.revoked').exists() or (
                status_path and Path(status_path).with_suffix('.revoked').exists()):
            return
        generation = events.snapshot()
        retry_after = CHECK_INTERVAL
        try:
            result = probe(port, mode)
        except ProbeUnavailable as error:
            result = {'allowed': False, 'reason': str(error)}
            retry_after = max(CHECK_INTERVAL, error.retry_after)
        except Exception:
            result = {'allowed': False, 'reason': 'Проверка IP недоступна. Сеть закрыта; повторяю проверку.'}
        if stop.is_set():
            return
        try:
            if not result.get('allowed') and result.get('country') != 'RU':
                # An outage/timeout is not evidence of a changed IP.
                for path in events.paths:
                    publish(path, result)
                if stop.wait(retry_after):
                    return
                continue
            result = policy.evaluate(result)
            if result.get('locked'):
                revoke(lease, status_path, result['reason'])
                for path in events.paths:
                    publish(path, result)
                return
            events.verified(generation, result)
        except OSError as error:
            # Never lose the monitor permanently on a Windows file-sharing
            # race. Pause independently of JSON replacement, then verify again.
            reason = 'Не удалось обновить сетевой статус. Сеть закрыта; повторяю проверку.'
            print(f'Network status update failed: {error}', file=sys.stderr, flush=True)
            try:
                events.pause(reason)
            except OSError:
                pass  # If the filesystem is unavailable, the short lease expires.
            if stop.wait(CHECK_INTERVAL):
                return
