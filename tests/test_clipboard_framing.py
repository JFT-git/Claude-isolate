import unittest

from windows.clipboard_framing import TEXT, PNG, pack_text, pack_png, parse
from windows.clipboard_image import dib_to_png, encode_png, png_to_dib


class ClipboardFramingTests(unittest.TestCase):
    def test_text_roundtrip(self):
        frame = pack_text('hello\nworld')
        messages, rest = parse(b'', frame)
        self.assertEqual(rest, b'')
        self.assertEqual(messages, [(TEXT, 'hello\nworld')])

    def test_png_roundtrip(self):
        payload = bytes(range(256))
        frame = pack_png(payload)
        messages, rest = parse(b'', frame)
        self.assertEqual(rest, b'')
        self.assertEqual(messages, [(PNG, payload)])

    def test_partial_reads(self):
        frame = pack_text('x') + pack_png(b'abc')
        messages, rest = parse(b'', frame[:3])
        self.assertEqual(messages, [])
        messages, rest = parse(rest, frame[3:])
        self.assertEqual(len(messages), 2)
        self.assertEqual(rest, b'')

    def test_rejects_oversized_length(self):
        with self.assertRaises(ValueError):
            parse(b'T' + b'\xff' * 4)

    def test_png_and_dib_roundtrip(self):
        rows = [bytes((255, 0, 0, 255, 0, 255, 0, 255)) , bytes((0, 0, 255, 255, 255, 255, 255, 255))]
        png = encode_png(2, 2, rows)
        dib = png_to_dib(png)
        restored = dib_to_png(dib)
        messages, rest = parse(b'', pack_png(restored))
        self.assertEqual(rest, b'')
        self.assertEqual(messages[0][0], PNG)
        self.assertTrue(messages[0][1].startswith(b'\x89PNG'))
        again = png_to_dib(messages[0][1])
        self.assertEqual(again[40:], dib[40:])


if __name__ == '__main__':
    unittest.main()
