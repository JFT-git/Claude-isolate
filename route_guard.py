"""Read-only Darwin routing notifications; never changes host network settings."""
import socket
import struct
import threading

# Darwin SDK net/route.h, rt_msghdr: version/type at 2/3, flags at 8.
ROUTE_CHANGES = {1, 2, 3}  # RTM_ADD, RTM_DELETE, RTM_CHANGE
INTERFACE_CHANGES = {12, 13, 14, 18}  # NEWADDR, DELADDR, IFINFO, IFINFO2
LINK_CACHE_FLAGS = 0x400 | 0x20000  # LLINFO / WASCLONED, not VPN route changes


def changed(data):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            return True  # malformed stream: fail closed
        length, version, kind = struct.unpack_from('=HBB', data, offset)
        if length < 4 or offset + length > len(data) or version != 5:
            return True
        if kind in INTERFACE_CHANGES:
            return True
        if kind in ROUTE_CHANGES:
            if length < 12:
                return True
            # Failed route commands do not change networking (e.g. EEXIST).
            if length >= 28 and struct.unpack_from('=i', data, offset + 24)[0]:
                offset += length
                continue
            flags = struct.unpack_from('=I', data, offset + 8)[0]
            if not flags & LINK_CACHE_FLAGS:
                return True
        offset += length
    return False


class RouteWatcher:
    def __init__(self, revoke, source=None, failure=None):
        self.revoke = revoke
        self.failure = failure or revoke
        self.source = source if source is not None else socket.socket(socket.AF_ROUTE, socket.SOCK_RAW, socket.AF_INET)
        self.source.settimeout(.1)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    data = self.source.recv(65535)
                except socket.timeout:
                    continue
                if not data:
                    self.failure('Наблюдение за сетью прервано. Нужен перезапуск среды.')
                    return
                if changed(data):
                    self.revoke('Проверяю подключение после сетевого события…')
        except Exception:
            if not self.stop_event.is_set():
                self.failure('Наблюдение за сетью прервано. Нужен перезапуск среды.')
        finally:
            self.source.close()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
