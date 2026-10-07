#!/usr/bin/env python3
"""Offline integration tests using tiny VSIX/tar fixtures and CLI mocks."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).with_name('update-vscode-codex-ext.sh')
OLD = '26.917.62051'
LATEST = '26.1000.1'
RUNTIME = '0.153.4'

CURL_MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
url = next(a for a in args if a.startswith('https://'))
root = pathlib.Path(os.environ['MOCK_ROOT'])
with (root / 'requests').open('a') as log:
    log.write(url + '\n')
if '--output' in args:
    shutil.copyfile(root / 'assets' / url.rsplit('/', 1)[1], args[args.index('--output') + 1])
else:
    data = json.loads((root / 'api.json').read_text())
    key = url.removeprefix('https://api.github.com/repos/')
    if key not in data:
        print('mock HTTP 404', file=sys.stderr)
        sys.exit(22)
    print(json.dumps(data[key]))
'''
CODE_MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, zipfile
root = pathlib.Path(os.environ['MOCK_ROOT'])
args = sys.argv[1:]
with (root / 'code-calls').open('a') as log:
    log.write(json.dumps(args) + '\n')
if '--install-extension' in args:
    vsix = pathlib.Path(args[args.index('--install-extension') + 1])
    assert vsix.suffix == '.vsix', 'Marketplace install is forbidden'
    # All downloads and executable smoke tests must precede installation.
    assert (root / 'runtime-probed').exists()
    assert (root / 'host-probed').exists()
    with zipfile.ZipFile(vsix) as archive:
        package = json.loads(archive.read('extension/package.json'))
        version = package['version']
        dest = pathlib.Path(os.environ['EXTENSIONS_DIR']) / f'openai.chatgpt-{version}-linux-x64'
        for name in archive.namelist():
            if name.startswith('extension/'):
                target = dest / name.removeprefix('extension/')
                target.parent.mkdir(parents=True, exist_ok=True)
                # Preserve updater backups; replace symlinks like a fresh install.
                if target.is_symlink():
                    target.unlink()
                target.write_bytes(archive.read(name))
    (root / 'installed').write_text(version)
elif '--list-extensions' in args:
    print('openai.chatgpt@' + os.environ.get('MOCK_INSTALLED_VERSION', (root / 'installed').read_text()))
else:
    sys.exit('unexpected code command')
'''


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-updater-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('assets', 'mock-bin', 'home', 'tmp'):
            (self.root / name).mkdir()
        for name, content in [('curl', CURL_MOCK), ('code', CODE_MOCK)]:
            path = self.root / 'mock-bin' / name
            path.write_text(content)
            path.chmod(0o755)
        self.env = {**os.environ, 'MOCK_ROOT': str(self.root),
                    'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
                    'PATH': str(self.root / 'mock-bin') + ':' + os.environ['PATH'],
                    'CODE_CMD': str(self.root / 'mock-bin/code'),
                    'LOCAL_BIN_DIR': str(self.root / 'home/bin with spaces'),
                    'EXTENSIONS_DIR': str(self.root / 'home/extensions')}
        self.env.pop('EXTENSION_VERSION', None)
        self.env.pop('GH_TOKEN', None)
        self.api = {}
        for version in (OLD, LATEST):
            name = f'openai.chatgpt-{version}-linux-x64-environment-variable-patch.vsix'
            with zipfile.ZipFile(self.root / 'assets' / name, 'w') as archive:
                archive.writestr('extension/package.json', json.dumps(
                    {'publisher': 'openai', 'name': 'chatgpt', 'version': version}))
                archive.writestr('extension/bin/linux-x86_64/codex', 'pristine bundled binary')
                if version == LATEST:
                    archive.writestr('extension/bin/linux-x86_64/codex-package.json',
                                     json.dumps({'version': '0.1.0'}))
            release = {'tag_name': version + '-environment-variable-patch',
                       'assets': [self.asset(name)]}
            self.api['TTTPOB/codex-vscode-env-patch/releases/tags/' + release['tag_name']] = release
            if version == LATEST:
                self.api['TTTPOB/codex-vscode-env-patch/releases/latest'] = release
        fork_name = f'codex-nfs-rust-v{RUNTIME}-x86_64-unknown-linux-musl.tar.gz'
        host_name = 'codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz'
        self.tar(fork_name, 'codex', f'#!/bin/sh\ntouch "$MOCK_ROOT/runtime-probed"\necho "codex-cli {RUNTIME}"\n')
        self.tar(host_name, 'codex-code-mode-host-x86_64-unknown-linux-musl',
                 '#!/bin/sh\ntouch "$MOCK_ROOT/host-probed"\nexit 0\n')
        valid = {'tag_name': 'nfs-rust-v' + RUNTIME, 'published_at': '2026-09-20',
                 'assets': [self.asset(fork_name)]}
        older_name = 'codex-nfs-rust-v0.1.0-x86_64-unknown-linux-musl.tar.gz'
        self.tar(older_name, 'codex', '#!/bin/sh\necho "codex-cli 0.1.0"\n')
        older = {**valid, 'tag_name': 'nfs-rust-v0.1.0', 'published_at': '2026-01-01',
                 'assets': [self.asset(older_name)]}
        self.api['TTTPOB/codex/releases?per_page=100&page=1'] = [
            {**valid, 'tag_name': 'rust-v9.0.0'}, {**valid, 'prerelease': True},
            {**valid, 'draft': True}, older, valid]
        self.api['openai/codex/releases/tags/rust-v' + RUNTIME] = {
            'tag_name': 'rust-v' + RUNTIME, 'assets': [self.asset(host_name)]}

    def asset(self, name):
        digest = hashlib.sha256((self.root / 'assets' / name).read_bytes()).hexdigest()
        return {'name': name, 'browser_download_url': 'https://fixtures/' + name,
                'digest': 'sha256:' + digest}

    def tar(self, name, binary, content):
        data = content.encode()
        with tarfile.open(self.root / 'assets' / name, 'w:gz') as archive:
            info = tarfile.TarInfo(binary)
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))

    def run_update(self, *args):
        (self.root / 'api.json').write_text(json.dumps(self.api))
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env,
                              text=True, capture_output=True, timeout=20)

    def assert_clean(self):
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])
        self.assertEqual(list((self.root / 'home').rglob('*.new.*')), [])
        self.assertEqual(list((self.root / 'home').rglob('.codex-link.*')), [])

    def assert_success(self, result, version):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        bundle = Path(self.env['EXTENSIONS_DIR']) / f'openai.chatgpt-{version}-linux-x64/bin/linux-x86_64'
        self.assertTrue((bundle / 'codex').is_symlink())
        self.assertEqual(os.readlink(bundle / 'codex'), self.env['LOCAL_BIN_DIR'] + '/codex_')
        self.assertEqual((bundle / 'codex-orig').read_text(), 'pristine bundled binary')
        local_host = Path(self.env['LOCAL_BIN_DIR']) / 'codex-code-mode-host'
        self.assertEqual(local_host.read_bytes(), (bundle / 'codex-code-mode-host').read_bytes())
        probe = subprocess.run([str(bundle / 'codex'), '--version'], env=self.env,
                               text=True, capture_output=True)
        self.assertEqual(probe.stdout.strip(), 'codex-cli ' + RUNTIME)
        self.assertEqual(probe.returncode, 0)
        self.assert_clean()

    def test_old_version_without_codex_package_and_rerun(self):
        self.assert_success(self.run_update('--version', OLD), OLD)
        self.assert_success(self.run_update('--version', OLD), OLD)
        self.assertIn('releases/tags/' + OLD + '-environment-variable-patch',
                      (self.root / 'requests').read_text())

    def test_latest_and_runtime_independent_of_bundled_version(self):
        self.assert_success(self.run_update(), LATEST)
        requests = (self.root / 'requests').read_text()
        self.assertIn('codex-vscode-env-patch/releases/latest', requests)
        self.assertIn('openai/codex/releases/tags/rust-v' + RUNTIME, requests)
        self.assertNotIn('rust-v0.1.0', requests)

    def test_runtime_release_pagination(self):
        key = 'TTTPOB/codex/releases?per_page=100&page=1'
        valid = self.api[key][-1]
        self.api[key] = [{**valid, 'prerelease': True}] * 100
        self.api['TTTPOB/codex/releases?per_page=100&page=2'] = [valid]
        self.assert_success(self.run_update(), LATEST)
        self.assertIn('releases?per_page=100&page=2', (self.root / 'requests').read_text())

    def test_environment_version(self):
        self.env['EXTENSION_VERSION'] = OLD
        self.assert_success(self.run_update(), OLD)

    def test_missing_historical_release_or_asset_before_install(self):
        key = 'TTTPOB/codex-vscode-env-patch/releases/tags/' + OLD + '-environment-variable-patch'
        for release in (None, {'tag_name': OLD + '-environment-variable-patch', 'assets': []}):
            with self.subTest(release=release):
                if release is None:
                    self.api.pop(key)
                else:
                    self.api[key] = release
                result = self.run_update('--version', OLD)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('gh workflow run patch-release.yml --repo TTTPOB/codex-vscode-env-patch -f version=' + OLD, result.stderr)
                self.assertFalse((self.root / 'code-calls').exists())
                self.assertFalse(Path(self.env['LOCAL_BIN_DIR']).exists())
                self.assert_clean()

    def test_bad_runtime_digest_before_install(self):
        self.api['openai/codex/releases/tags/rust-v' + RUNTIME]['assets'][0]['digest'] = 'sha256:' + '0' * 64
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SHA-256 mismatch', result.stderr)
        self.assertFalse((self.root / 'code-calls').exists())
        self.assert_clean()

    def test_installed_version_mismatch_does_not_replace_runtime(self):
        self.env['MOCK_INSTALLED_VERSION'] = '99.0.0'
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('installed extension version is not', result.stderr)
        self.assertFalse(Path(self.env['LOCAL_BIN_DIR']).exists())
        self.assert_clean()

    def test_help_has_no_download_or_write(self):
        result = subprocess.run(['bash', str(SCRIPT), '--help'], env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.root / 'requests').exists())
        self.assertFalse((self.root / 'code-calls').exists())
        self.assertEqual(list((self.root / 'home').iterdir()), [])
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
