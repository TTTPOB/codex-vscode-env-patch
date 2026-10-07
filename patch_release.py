#!/usr/bin/env python3
"""Download, patch and release Codex VSIX packages using only the standard library."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

GALLERY = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
# Match the adjacent debug save/load functions, not unrelated release constants.
PAIR = re.compile(
    rf'(function\s+{IDENTIFIER}\((?P<arg>{IDENTIFIER})\)\{{'
    rf'(?P=arg)\?process\.env\.DEBUG=(?P=arg):delete)\s*'
    r'(?P<delete>"release"|process\.env\.DEBUG)'
    rf'(\}}function\s+{IDENTIFIER}\(\)\{{return)\s*'
    r'(?P<load>"release"|process\.env\.DEBUG)(\})'
)


def patch_source(source):
    count = 0

    def replace(match):
        nonlocal count
        if match['delete'] == '"release"' or match['load'] == '"release"':
            count += 1
        return (match[1] + " process.env.DEBUG" + match[4]
                + " process.env.DEBUG" + match[6])

    patched, recognized = PAIR.subn(replace, source)
    if not recognized:
        raise RuntimeError("Unrecognized debug save/load layout; refusing to publish")
    if re.search(r'delete\s*"release"', patched):
        raise RuntimeError("Unpatched delete release expression remains")
    return patched, count


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True).strip()


def marketplace_versions():
    payload = {"filters": [{"criteria": [{"filterType": 7, "value": "openai.chatgpt"}]}],
               "flags": 147}
    request = urllib.request.Request(
        GALLERY, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json;api-version=7.1-preview.1",
                 "User-Agent": "codex-vscode-env-patch"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)["results"][0]["extensions"][0]["versions"]


def prerelease(entry):
    return any(p["key"] == "Microsoft.VisualStudio.Code.PreRelease"
               and p["value"] == "true" for p in entry.get("properties", []))


def select_versions(entries, version, channel):
    candidates = [v for v in entries if (v["version"] == version if version else
                  prerelease(v) == (channel == "prerelease"))]
    if not candidates:
        raise RuntimeError(f"No Marketplace version found: {version or channel}")
    # Marketplace returns newest publications first; do not compare calendar-like versions.
    selected = candidates[0]["version"]
    return [v for v in candidates if v["version"] == selected]


def patch_vsix(entry, directory):
    version = entry["version"]
    platform = entry.get("targetPlatform") or "universal"
    asset = next(f for f in entry["files"]
                 if f["assetType"] == "Microsoft.VisualStudio.Services.VSIXPackage")
    output = directory / f"openai.chatgpt-{version}-{platform}-environment-variable-patch.vsix"
    original = directory / f"original-{platform}.vsix"
    print(f"Downloading {version} / {platform}", flush=True)
    with urllib.request.urlopen(asset["source"], timeout=180) as response, original.open("wb") as target:
        shutil.copyfileobj(response, target)
    with zipfile.ZipFile(original) as source:
        manifest = json.loads(source.read("extension/package.json"))
        if (manifest["publisher"], manifest["name"], manifest["version"]) != ("openai", "chatgpt", version):
            raise RuntimeError("Unexpected VSIX identity/version")
        bundle_path = "extension/" + manifest["main"].removeprefix("./")
        bundle = source.read(bundle_path).decode("utf-8")
        patched, count = patch_source(bundle)
        subprocess.run(["node", "--check"], input=patched, text=True, check=True)
        with zipfile.ZipFile(output, "w") as target:
            for info in source.infolist():
                # Preserve the original manifest, version, platform and executable permissions.
                data = patched.encode("utf-8") if info.filename == bundle_path else source.read(info)
                target.writestr(info, data)
    original.unlink()
    print(f"Validated {output.name}: {count} debug pair(s) patched", flush=True)
    return output, count, asset["source"]


def existing_release(repo, tag):
    result = subprocess.run(["gh", "api", f"repos/{repo}/releases/tags/{tag}"],
                            text=True, capture_output=True)
    if result.returncode == 0:
        return json.loads(result.stdout)
    if "HTTP 404" in result.stderr:
        return None
    raise RuntimeError(result.stderr)


def release(entries, repo, directory):
    version = entries[0]["version"]
    tag = f"{version}-environment-variable-patch"
    previous = existing_release(repo, tag)
    names = {a["name"] for a in previous["assets"]} if previous else set()
    expected = {f"openai.chatgpt-{version}-{e.get('targetPlatform') or 'universal'}-environment-variable-patch.vsix"
                for e in entries}
    if previous and not previous["draft"] and expected <= names:
        print(f"Already released: {tag}")
        return
    # Build all missing assets before creating or updating the release.
    built = [patch_vsix(e, directory) for e in entries
             if f"openai.chatgpt-{version}-{e.get('targetPlatform') or 'universal'}-environment-variable-patch.vsix" not in names]
    notes = (
        f"Unofficial Codex VS Code extension {version} — environment variable patch.\n\n"
        "Fixes the bundled debug save/load functions that inject DEBUG=release into the shared "
        "extension host. Extension identity and internal version are unchanged. "
        "No terminal environment settings are modified.\n\n"
        "Install the VSIX matching your extension host OS/architecture via Extensions: Install from VSIX, "
        "then restart VS Code. Disable automatic updates for this extension to avoid Marketplace "
        "replacing the patched bundle. These modified packages are not signed by OpenAI.\n\n"
        "Upstream issue: https://github.com/openai/codex/issues/13694\n\n"
        "Assets validated in this run:\n" + "\n".join(
            f"- {path.name}: {count} debug pair(s) patched; source: {url}"
            for path, count, url in built))
    if not previous:
        args = ["release", "create", tag, "--repo", repo, "--draft",
                "--title", f"{version} environment variable patch", "--notes", notes]
        if prerelease(entries[0]):
            args.append("--prerelease")
        gh(*args)
    for path, _, _ in built:
        gh("release", "upload", tag, str(path), "--repo", repo)
    if previous and previous["draft"]:
        gh("release", "edit", tag, "--repo", repo, "--notes", notes)
    gh("release", "edit", tag, "--repo", repo, "--draft=false")
    print(f"Published https://github.com/{repo}/releases/tag/{tag}")


def self_test():
    source = 'function save($t){$t?process.env.DEBUG=$t:delete"release"}function load(){return"release"}'
    patched, count = patch_source(source + ';function other(){return"release"}')
    assert count == 1 and 'function other(){return"release"}' in patched
    assert patch_source(patched) == (patched, 0)
    try:
        patch_source('function other(){return"release"}')
    except RuntimeError:
        pass
    else:
        raise AssertionError("Unknown bundles must fail")
    # Execute the actual repaired functions with an isolated environment object.
    program = patched + """;
const assert = require('node:assert/strict');
for (const initial of [undefined, 'my-app:*']) {
  const env = initial === undefined ? {} : {DEBUG: initial};
  const vm = require('node:vm');
  const ctx = {process: {env}};
  vm.runInNewContext(SOURCE + ';save(load());', ctx);
  assert.equal(env.DEBUG, initial);
  vm.runInNewContext("save('custom:*')", ctx);
  assert.equal(env.DEBUG, 'custom:*');
  vm.runInNewContext("save('')", ctx);
  assert.equal(Object.hasOwn(env, 'DEBUG'), false);
}
"""
    program = "const SOURCE = " + json.dumps(patched) + ";" + program
    subprocess.run(["node", "-e", program], check=True)
    print("Self-test passed: absent/present DEBUG, save/delete, idempotence and scoped matching")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="", help="Exact upstream version; blank selects latest")
    parser.add_argument("--channel", choices=["stable", "prerelease"], default="stable")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--build-only", action="store_true", help="Build without publishing")
    parser.add_argument("--platform", help="Build-only platform filter")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.build_only and not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required for publishing")
    if args.platform and not args.build_only:
        parser.error("--platform is only supported with --build-only")
    entries = select_versions(marketplace_versions(), args.version, args.channel)
    if args.platform:
        entries = [e for e in entries if (e.get("targetPlatform") or "universal") == args.platform]
        if not entries:
            parser.error("Selected version does not have that platform")
    print(f"Selected {entries[0]['version']}: {len(entries)} platform(s)", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.build_only:
        for entry in entries:
            patch_vsix(entry, args.output)
    else:
        with tempfile.TemporaryDirectory(dir=args.output) as directory:
            release(entries, args.repo, Path(directory))


if __name__ == "__main__":
    main()
