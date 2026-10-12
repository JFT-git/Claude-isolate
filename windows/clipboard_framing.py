"""Length-prefixed clipboard messages shared by host and guest bridges."""
import struct

TEXT = b'T'
PNG = b'P'


def pack(kind, payload):
    if kind not in (TEXT, PNG):
        raise ValueError('Unsupported clipboard kind')
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError('payload must be bytes')
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError('clipboard payload too large')
    return kind + struct.pack('>I', len(payload)) + bytes(payload)


def pack_text(text):
    return pack(TEXT, text.encode('utf-8'))


def pack_png(png):
    return pack(PNG, png)


def parse(buffer, chunk=b''):
    """Return (messages, remainder) after appending chunk to buffer."""
    buffer = bytes(buffer) + bytes(chunk)
    messages = []
    while len(buffer) >= 5:
        kind = buffer[:1]
        if kind not in (TEXT, PNG):
            raise ValueError('Invalid clipboard frame')
        length = struct.unpack('>I', buffer[1:5])[0]
        if length > 16 * 1024 * 1024:
            raise ValueError('Invalid clipboard length')
        if len(buffer) < 5 + length:
            break
        payload = buffer[5:5 + length]
        buffer = buffer[5 + length:]
        if kind == TEXT:
            messages.append((TEXT, payload.decode('utf-8')))
        else:
            messages.append((PNG, payload))
    return messages, buffer
