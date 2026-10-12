"""PNG and Windows DIB conversion for the clipboard bridge.

Stdlib only, so the host can exchange images with xclip without Pillow.
"""
import struct
import zlib


def _chunk(tag, data):
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)


def encode_png(width, height, rows):
    """Encode top-down RGBA rows as an 8-bit PNG."""
    raw = b''.join(b'\x00' + row for row in rows)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', zlib.compress(raw, 9)) + _chunk(b'IEND', b'')


def _paeth(left, up, up_left):
    estimate = left + up - up_left
    if abs(estimate - left) <= abs(estimate - up) and abs(estimate - left) <= abs(estimate - up_left):
        return left
    if abs(estimate - up) <= abs(estimate - up_left):
        return up
    return up_left


def _unfilter(data, width, bpp):
    stride = width * bpp
    rows = []
    previous = bytearray(stride)
    index = 0
    while index < len(data):
        if index + 1 + stride > len(data):
            raise ValueError('Truncated PNG scanline')
        kind = data[index]
        index += 1
        row = bytearray(data[index:index + stride])
        index += stride
        for column in range(stride):
            left = row[column - bpp] if column >= bpp else 0
            up = previous[column]
            up_left = previous[column - bpp] if column >= bpp else 0
            if kind == 0:
                extra = 0
            elif kind == 1:
                extra = left
            elif kind == 2:
                extra = up
            elif kind == 3:
                extra = (left + up) // 2
            elif kind == 4:
                extra = _paeth(left, up, up_left)
            else:
                raise ValueError('Unsupported PNG filter')
            row[column] = (row[column] + extra) & 255
        rows.append(bytes(row))
        previous = row
    return rows


def decode_png(png):
    """Return (width, height, top-down RGBA rows)."""
    if not png.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Not a PNG')
    position = 8
    compressed = b''
    width = height = color = None
    while position + 8 <= len(png):
        length = struct.unpack('>I', png[position:position + 4])[0]
        if position + 12 + length > len(png):
            raise ValueError('Truncated PNG chunk')
        tag = png[position + 4:position + 8]
        data = png[position + 8:position + 8 + length]
        position += 12 + length
        if tag == b'IHDR':
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', data)
            if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
                raise ValueError('Unsupported PNG')
            if width <= 0 or height <= 0 or width > 8192 or height > 8192:
                raise ValueError('PNG dimensions are out of range')
        elif tag == b'IDAT':
            compressed += data
        elif tag == b'IEND':
            break
    if width is None or not compressed:
        raise ValueError('Incomplete PNG')
    channels = 4 if color == 6 else 3
    rows = _unfilter(zlib.decompress(compressed), width, channels)
    if len(rows) != height:
        raise ValueError('PNG height does not match scanlines')
    if color == 2:
        rows = [b''.join(bytes((row[i], row[i + 1], row[i + 2], 255))
                         for i in range(0, len(row), 3)) for row in rows]
    return width, height, rows


def png_to_dib(png):
    """Bottom-up 32-bit BI_RGB DIB, the format Windows CF_DIB expects."""
    width, height, rows = decode_png(png)
    header = struct.pack('<IiiHHIIiiII', 40, width, height, 1, 32, 0,
                         width * height * 4, 0, 0, 0, 0)
    pixels = bytearray()
    for row in reversed(rows):
        for index in range(0, len(row), 4):
            red, green, blue, alpha = row[index:index + 4]
            pixels += bytes((blue, green, red, alpha))
    return header + bytes(pixels)


def dib_to_png(dib):
    """Convert a CF_DIB or CF_DIBV5 bitmap into PNG, or return None."""
    if len(dib) < 40:
        return None
    size, width, height, planes, bitcount, compression = struct.unpack_from('<IiiHHI', dib, 0)
    if width <= 0 or planes != 1 or bitcount not in (24, 32) or size < 40 or size > len(dib):
        return None
    top_down = height < 0
    height = abs(height)
    if height <= 0 or width > 8192 or height > 8192 or compression not in (0, 3):
        return None
    offset = size
    if compression == 3 and size == 40:
        offset += 12
    channels = bitcount // 8
    stride = ((width * bitcount + 31) // 32) * 4
    if offset + stride * height > len(dib):
        return None
    rows = []
    for row_index in range(height):
        source = row_index if top_down else height - 1 - row_index
        pixel = dib[offset + source * stride:offset + source * stride + width * channels]
        rgba = bytearray()
        if channels == 4:
            for index in range(0, len(pixel), 4):
                blue, green, red, alpha = pixel[index:index + 4]
                rgba += bytes((red, green, blue, 255 if alpha == 0 else alpha))
        else:
            for index in range(0, len(pixel), 3):
                blue, green, red = pixel[index:index + 3]
                rgba += bytes((red, green, blue, 255))
        rows.append(bytes(rgba))
    return encode_png(width, height, rows)
