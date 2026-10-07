import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import release_image


class Response(io.BytesIO):
    def __init__(self, content, status=200, headers=None):
        super().__init__(content)
        self.status = status
        self.headers = headers or {}
        self.url = 'https://release-assets.githubusercontent.com/image'


class ReleaseImageTests(unittest.TestCase):
    def manifest(self, content):
        return dict(schema=1, arch='x86_64', release='v0.3.16',
                    filename='Claude-isolate-guest-x86_64.qcow2', size=len(content),
                    sha256=hashlib.sha256(content).hexdigest())

    def test_resumes_interrupted_transfer_and_verifies_before_publication(self):
        content = b'complete verified disk'
        manifest = self.manifest(content)
        with tempfile.TemporaryDirectory() as folder:
            partial = Path(folder) / (manifest['sha256'] + '.partial')
            partial.write_bytes(content[:5])
            response = Response(content[5:], 206, {'Content-Range': f'bytes 5-{len(content)-1}/{len(content)}'})
            with patch.object(release_image, 'metadata', return_value=manifest), patch.object(release_image.urllib.request, 'urlopen', return_value=response) as fetch:
                image, digest = release_image.download(folder, 'x86_64')
                self.assertEqual(fetch.call_args.args[0].get_header('Range'), 'bytes=5-')
                self.assertEqual(image.read_bytes(), content)
                self.assertEqual(digest, manifest['sha256'])
                self.assertFalse(partial.exists())
            with patch.object(release_image, 'metadata', return_value=manifest), patch.object(release_image.urllib.request, 'urlopen') as fetch:
                release_image.download(folder, 'x86_64')
                fetch.assert_not_called()

    def test_server_ignoring_range_replaces_instead_of_appending(self):
        content = b'correct image'
        manifest = self.manifest(content)
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / (manifest['sha256'] + '.partial')).write_bytes(b'old')
            with patch.object(release_image, 'metadata', return_value=manifest), patch.object(release_image.urllib.request, 'urlopen', return_value=Response(content)):
                image, _ = release_image.download(folder, 'x86_64')
                self.assertEqual(image.read_bytes(), content)

    def test_wrong_digest_is_never_published(self):
        manifest = self.manifest(b'good')
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(release_image, 'metadata', return_value=manifest), patch.object(release_image.urllib.request, 'urlopen', return_value=Response(b'evil')):
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    release_image.download(folder, 'x86_64')
            self.assertFalse(list(Path(folder).glob('*.qcow2')))

    def test_bad_range_does_not_modify_partial(self):
        manifest = self.manifest(b'good image')
        with tempfile.TemporaryDirectory() as folder:
            partial = Path(folder) / (manifest['sha256'] + '.partial')
            partial.write_bytes(b'good')
            with patch.object(release_image, 'metadata', return_value=manifest), patch.object(release_image.urllib.request, 'urlopen', return_value=Response(b'bad', 206, {'Content-Range': 'bytes 0-2/10'})):
                with self.assertRaisesRegex(ValueError, 'range'):
                    release_image.download(folder, 'x86_64')
            self.assertEqual(partial.read_bytes(), b'good')

    def test_manifest_cannot_redirect_to_other_repo_or_path(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = self.manifest(b'image')
            path = Path(folder) / 'guest-image-x86_64.json'
            for key, value in [('release', '../evil'), ('filename', '../../file'), ('arch', 'aarch64'), ('size', 2**32)]:
                path.write_text(json.dumps(dict(manifest, **{key: value})))
                with patch.object(release_image, 'ROOT', Path(folder)):
                    with self.assertRaises(ValueError):
                        release_image.metadata('x86_64')
