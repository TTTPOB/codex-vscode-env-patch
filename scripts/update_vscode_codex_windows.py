#!/usr/bin/env python3
"""Install the latest patched Windows VSIX and bundle official stable Codex.

Run on Windows: python scripts/update_vscode_codex_windows.py
Only Python's standard library and an installed VS Code CLI are required.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

FORK_REPO = "TTTPOB/codex-vscode-env-patch"
OFFICIAL_REPO = "openai/codex"
EXTENSION_ID = "openai.chatgpt"
TARGETS = {"x64": "x86_64", "arm64": "aarch64"}
HELPERS = ("codex-command-runner.exe", "codex-windows-sandbox-setup.exe")
HEADERS = {"User-Agent": "codex-vscode-env-patch-windows"}


def detect_arch(override=None):
    """Prefer native Windows architecture, including an emulated Python process."""
    if override:
        return override
    machine = (os.environ.get("PROCESSOR_ARCHITEW6432")
               or os.environ.get("PROCESSOR_ARCHITECTURE")
               or platform.machine()).lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("amd64", "x86_64", "x64"):
        return "x64"
    raise RuntimeError(f"Unsupported Windows architecture: {machine}; use --arch x64 or arm64")


def github_latest(repo):
    """Use public GitHub endpoints; never attach credentials to download redirects."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={**HEADERS, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.load(response)
    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError(f"{repo}: latest release is not stable")
    return release


def select_asset(release, pattern):
    matches = [asset for asset in release.get("assets", [])
               if re.fullmatch(pattern, asset["name"])]
    if len(matches) != 1:
        raise RuntimeError(
            f"Release {release.get('tag_name')} must contain one asset matching {pattern}; "
            f"found {len(matches)}")
    return matches[0]


def download(asset, destination):
    """Stream large files with periodic progress and without loading them into memory."""
    print(f"Downloading {asset['name']} ...", flush=True)
    request = urllib.request.Request(asset["browser_download_url"], headers=HEADERS)
    downloaded = 0
    last_report = time.monotonic()
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        total = int(response.headers.get("Content-Length") or asset.get("size") or 0)
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            downloaded += len(chunk)
            if time.monotonic() - last_report >= 2:
                progress = f"{downloaded / (1024 * 1024):.1f} MiB"
                if total:
                    progress += f" / {total / (1024 * 1024):.1f} MiB ({downloaded * 100 / total:.0f}%)"
                print(f"  {progress}", flush=True)
                last_report = time.monotonic()
    if total and downloaded != total:
        raise RuntimeError(f"Incomplete download: {asset['name']} ({downloaded}/{total} bytes)")
    print(f"  Downloaded {downloaded / (1024 * 1024):.1f} MiB", flush=True)


def extract_package(archive, destination):
    """Extract a Windows runtime package, rejecting paths outside its temporary directory."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        for member in members:
            target = (root / member.name).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"Invalid package path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"Unexpected link or special file in Windows package: {member.name}")
        # Paths and member types were checked above; keep Python 3.9+ support.
        options = {"filter": "fully_trusted"} if hasattr(tarfile, "data_filter") else {}
        package.extractall(destination, members=members, **options)
    return destination


def validate_runtime(package):
    metadata_path = package / "codex-package.json"
    if not metadata_path.is_file():
        raise RuntimeError("Official archive is not a complete Codex package: missing codex-package.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if (metadata.get("layoutVersion") != 1
            or metadata.get("entrypoint") != "bin/codex.exe"
            or metadata.get("resourcesDir") != "codex-resources"
            or metadata.get("pathDir") != "codex-path"):
        raise RuntimeError(f"Unsupported official Codex package layout: {metadata}")
    required = ["bin/codex.exe", "bin/codex-code-mode-host.exe", "codex-path/rg.exe"]
    required += [f"codex-resources/{name}" for name in HELPERS]
    for name in required:
        if not (package / name).is_file():
            raise RuntimeError(f"Incomplete official Codex package: missing {name}")
    return metadata


def validate_vsix(vsix, bundle_name):
    prefix = f"extension/bin/{bundle_name}/"
    with zipfile.ZipFile(vsix) as archive:
        names = set(archive.namelist())
        for name in ("extension/package.json", prefix + "codex.exe"):
            if name not in names:
                raise RuntimeError(f"Patched VSIX has unexpected layout: missing {name}")
        manifest = json.loads(archive.read("extension/package.json"))
        if f"{manifest.get('publisher')}.{manifest.get('name')}" != EXTENSION_ID:
            raise RuntimeError("Downloaded VSIX is not the openai.chatgpt extension")
        metadata_name = prefix + "codex-package.json"
        bundled_version = (json.loads(archive.read(metadata_name)).get("version", "unknown")
                           if metadata_name in names else "unknown")
    return bundled_version


def code_cli(code, args):
    """Windows batch launchers need cmd.exe; list2cmdline preserves spaced paths."""
    command = [str(code), *map(str, args)]
    batch = str(code).lower().endswith((".cmd", ".bat"))
    result = subprocess.run(
        subprocess.list2cmdline(command) if batch else command,
        shell=batch, check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def install_runtime(package, bundle):
    """Flatten official bin/ and merge resources without deleting extension-only files."""
    if not (bundle / "codex.exe").is_file():
        raise RuntimeError(f"Installed extension has no Windows Codex bundle: {bundle}")
    metadata = json.loads((package / "codex-package.json").read_text(encoding="utf-8"))
    try:
        shutil.copytree(package / "bin", bundle, dirs_exist_ok=True)
        for directory in ("codex-path", "codex-resources"):
            shutil.copytree(package / directory, bundle / directory, dirs_exist_ok=True)
        # The extension also addresses these executables directly at the bundle root.
        shutil.copy2(package / "codex-path" / "rg.exe", bundle / "rg.exe")
        for name in HELPERS:
            shutil.copy2(package / "codex-resources" / name, bundle / name)
        metadata["entrypoint"] = "codex.exe"
        (bundle / "codex-package.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    except (PermissionError, shutil.Error) as error:
        raise RuntimeError(
            "Cannot replace the installed Codex runtime: an executable may be in use. "
            "Close Codex and all VS Code windows, then rerun this command in an external "
            f"PowerShell or Command Prompt. No processes were terminated. Details: {error}"
        ) from error


def update(arch, code, extensions_dir=None):
    target = TARGETS[arch]
    bundle_name = f"windows-{target}"
    print(f"Checking latest patched Windows {arch} extension and official stable Codex ...", flush=True)
    fork_release = github_latest(FORK_REPO)
    official_release = github_latest(OFFICIAL_REPO)
    if not re.fullmatch(r"rust-v\d+\.\d+\.\d+", official_release["tag_name"]):
        raise RuntimeError(f"Unexpected official stable tag: {official_release['tag_name']}")
    vsix_asset = select_asset(
        fork_release, rf"openai\.chatgpt-[\d.]+-win32-{arch}-environment-variable-patch\.vsix")
    package_asset = select_asset(
        official_release, rf"codex-package-{target}-pc-windows-msvc\.tar\.gz")
    cli_options = ["--extensions-dir", str(extensions_dir)] if extensions_dir else []
    with tempfile.TemporaryDirectory(prefix="codex-windows-update-") as temporary:
        work = Path(temporary)
        vsix = work / vsix_asset["name"]
        archive = work / package_asset["name"]
        download(vsix_asset, vsix)
        download(package_asset, archive)
        print("Validating VSIX and unpacking the complete official runtime ...", flush=True)
        bundled_version = validate_vsix(vsix, bundle_name)
        package = extract_package(archive, work / "runtime")
        metadata = validate_runtime(package)
        print(f"Patched extension: {fork_release['tag_name']}", flush=True)
        print(f"Codex: bundled {bundled_version} -> official stable {metadata['version']}", flush=True)
        print("Installing patched VSIX ...", flush=True)
        output = code_cli(code, [*cli_options, "--install-extension", str(vsix), "--force"])
        if output:
            print(output, flush=True)
        located = code_cli(code, [*cli_options, "--locate-extension", EXTENSION_ID])
        extension = Path(located)
        if not located or not extension.is_dir():
            raise RuntimeError(f"VS Code did not return an installed extension directory: {located!r}")
        bundle = extension / "bin" / bundle_name
        print(f"Replacing bundled runtime in {bundle} ...", flush=True)
        install_runtime(package, bundle)
    print("Done. Reload VS Code. Disable Codex extension auto-update to keep the patch.", flush=True)
    print("If chatgpt.cliExecutable is configured, clear it to use this bundled runtime.", flush=True)
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=TARGETS, help="Windows architecture (default: auto-detect native architecture)")
    parser.add_argument("--code", default="code", help="VS Code CLI command or full path to code.cmd")
    parser.add_argument("--extensions-dir", help="Use this VS Code extensions directory for install and locate")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("This script is for Windows only; run it with Python on your Windows machine")
    try:
        code = shutil.which(args.code)
        if not code:
            raise RuntimeError("VS Code CLI not found. Add code to PATH or pass --code 'C:\\path\\to\\code.cmd'")
        update(detect_arch(args.arch), code, args.extensions_dir)
        return 0
    except subprocess.CalledProcessError as error:
        print(f"VS Code CLI failed: {error.stderr or error.stdout or error}", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
