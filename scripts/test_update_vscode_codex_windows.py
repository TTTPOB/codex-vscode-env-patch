#!/usr/bin/env python3
"""Offline tests for the Windows updater's real archive and installation paths."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location(
    "windows_updater", Path(__file__).with_name("update_vscode_codex_windows.py")
)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class WindowsUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="windows-updater-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata = {
            "layoutVersion": 1,
            "version": "0.161.0",
            "target": "x86_64-pc-windows-msvc",
            "variant": "codex",
            "entrypoint": "bin/codex.exe",
            "resourcesDir": "codex-resources",
            "pathDir": "codex-path",
        }
        self.files = {
            "codex-package.json": json.dumps(self.metadata).encode(),
            "bin/codex.exe": b"official codex",
            "bin/codex-code-mode-host.exe": b"official host",
            "codex-path/rg.exe": b"official rg",
            "codex-resources/codex-command-runner.exe": b"official runner",
            "codex-resources/codex-windows-sandbox-setup.exe": b"official setup",
            "codex-resources/voice/bin/codex-voice-host.exe": b"official voice",
        }
        self.archive = self.root / "official.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, content in self.files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))

    def test_official_archive_installs_flat_bundle_with_matching_resources(self):
        package = updater.extract_package(self.archive, self.root / "extracted")
        self.assertEqual(updater.validate_runtime(package), self.metadata)
        bundle = self.root / "VS Code with spaces" / "bin" / "windows-x86_64"
        bundle.mkdir(parents=True)
        (bundle / "codex.exe").write_bytes(b"old cli")
        (bundle / "codex-resources" / "extension-only").mkdir(parents=True)
        preserved = bundle / "codex-resources" / "extension-only" / "keep.txt"
        preserved.write_text("extension resource")
        updater.install_runtime(package, bundle)
        self.assertEqual((bundle / "codex.exe").read_bytes(), b"official codex")
        self.assertEqual((bundle / "codex-code-mode-host.exe").read_bytes(), b"official host")
        for path, expected in {
            "rg.exe": b"official rg",
            "codex-command-runner.exe": b"official runner",
            "codex-windows-sandbox-setup.exe": b"official setup",
            "codex-path/rg.exe": b"official rg",
            "codex-resources/codex-command-runner.exe": b"official runner",
            "codex-resources/codex-windows-sandbox-setup.exe": b"official setup",
            "codex-resources/voice/bin/codex-voice-host.exe": b"official voice",
        }.items():
            self.assertEqual((bundle / path).read_bytes(), expected)
        expected_metadata = {**self.metadata, "entrypoint": "codex.exe"}
        self.assertEqual(json.loads((bundle / "codex-package.json").read_text()), expected_metadata)
        self.assertEqual(preserved.read_text(), "extension resource")
        self.assertFalse((bundle / "codex_").exists())
        self.assertFalse((bundle / "codex-orig").exists())
        self.assertFalse(any(p.is_symlink() for p in bundle.rglob("*")))
        updater.install_runtime(package, bundle)
        self.assertEqual((bundle / "codex.exe").read_bytes(), b"official codex")

    def test_missing_helper_rejected_before_install(self):
        package = updater.extract_package(self.archive, self.root / "extracted")
        (package / "codex-resources" / "codex-windows-sandbox-setup.exe").unlink()
        with self.assertRaises((RuntimeError, ValueError, FileNotFoundError)):
            updater.validate_runtime(package)

    def test_architecture_detection_and_override(self):
        with patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": "AMD64"}, clear=True), \
                patch.object(updater.platform, "machine", return_value="AMD64"):
            self.assertEqual(updater.detect_arch(), "x64")
            self.assertEqual(updater.detect_arch("arm64"), "arm64")
        with patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": "ARM64", "PROCESSOR_ARCHITECTURE": "x86"}, clear=True), \
                patch.object(updater.platform, "machine", return_value="x86"):
            self.assertEqual(updater.detect_arch(), "arm64")

    def test_release_assets_select_requested_windows_architecture(self):
        release = {"assets": [
            {"name": "codex-package-aarch64-pc-windows-msvc.tar.gz"},
            {"name": "codex-package-x86_64-pc-windows-msvc.tar.gz"},
            {"name": "codex-x86_64-pc-windows-msvc.exe"},
        ]}
        selected = updater.select_asset(release, r"codex-package-x86_64-pc-windows-msvc\.tar\.gz")
        self.assertEqual(selected["name"], "codex-package-x86_64-pc-windows-msvc.tar.gz")

    def test_update_installs_fork_vsix_then_official_runtime_and_cleans_downloads(self):
        version = "26.1002.51308"
        vsix_name = f"openai.chatgpt-{version}-win32-x64-environment-variable-patch.vsix"
        official_name = "codex-package-x86_64-pc-windows-msvc.tar.gz"
        vsix = self.root / "fixture.vsix"
        with zipfile.ZipFile(vsix, "w") as archive:
            archive.writestr("extension/package.json", json.dumps({
                "publisher": "openai", "name": "chatgpt", "version": version,
            }))
            archive.writestr("extension/bin/windows-x86_64/codex.exe", b"bundled cli")
        releases = [
            {"tag_name": version + "-environment-variable-patch", "assets": [{"name": vsix_name}]},
            {"tag_name": "rust-v0.161.0", "assets": [{"name": official_name}]},
        ]
        extension = self.root / "extensions with spaces" / ("openai.chatgpt-" + version)
        bundle = extension / "bin" / "windows-x86_64"
        calls = []
        downloads = []

        def download(asset, destination):
            downloads.append(destination)
            shutil.copyfile(vsix if asset["name"].endswith(".vsix") else self.archive, destination)

        def code_cli(code, args):
            calls.append(args)
            self.assertEqual(args[:2], ["--extensions-dir", str(extension.parent)])
            if "--install-extension" in args:
                # The runtime must have been prepared before changing the installed extension.
                self.assertTrue((downloads[-1].parent / "runtime/bin/codex.exe").is_file())
                bundle.mkdir(parents=True)
                (bundle / "codex.exe").write_bytes(b"bundled cli")
                return "Installed"
            self.assertEqual(args[2:], ["--locate-extension", "openai.chatgpt"])
            return str(extension)

        with patch.object(updater, "github_latest", side_effect=releases) as latest, \
                patch.object(updater, "download", side_effect=download), \
                patch.object(updater, "code_cli", side_effect=code_cli):
            result = updater.update("x64", "code.cmd", str(extension.parent))
        self.assertEqual(result, bundle)
        self.assertEqual([call.args[0] for call in latest.call_args_list], [
            "TTTPOB/codex-vscode-env-patch", "openai/codex",
        ])
        self.assertEqual(len(calls), 2)
        self.assertEqual((bundle / "codex.exe").read_bytes(), b"official codex")
        self.assertFalse(downloads[0].parent.exists())

    def test_locked_executable_has_actionable_error(self):
        package = updater.extract_package(self.archive, self.root / "extracted")
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "codex.exe").write_bytes(b"running executable")
        error = shutil.Error([("source", "codex.exe", "Permission denied")])
        with patch.object(updater.shutil, "copytree", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "Close Codex and all VS Code windows"):
                updater.install_runtime(package, bundle)

    def test_batch_cli_preserves_spaced_paths(self):
        command = r"C:\Program Files\Microsoft VS Code\bin\code.cmd"
        vsix = r"C:\Users\Test User\Temp\patched.vsix"
        with patch.object(updater.subprocess, "run") as run:
            run.return_value.stdout = "installed\n"
            self.assertEqual(updater.code_cli(command, ["--install-extension", vsix]), "installed")
        args, kwargs = run.call_args
        self.assertEqual(args[0], f'"{command}" --install-extension "{vsix}"')
        self.assertTrue(kwargs["shell"])
        self.assertTrue(kwargs["check"])


if __name__ == "__main__":
    unittest.main()
