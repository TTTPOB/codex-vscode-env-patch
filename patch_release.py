#!/usr/bin/env python3
"""Download, patch and release Codex VSIX packages using only the standard library."""

import argparse
from copy import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
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
        if match['delete'] != '"release"' and match['load'] != '"release"':
            return match[0]
        count += 1
        return (match[1] + " process.env.DEBUG" + match[4]
                + " process.env.DEBUG" + match[6])

    patched, recognized = PAIR.subn(replace, source)
    if not recognized:
        raise RuntimeError("Unrecognized debug save/load layout; refusing to publish")
    if re.search(r'delete\s*"release"', patched):
        raise RuntimeError("Unpatched delete release expression remains")
    return patched, count


def validate_debug_behavior(source):
    pairs = [match[0] for match in PAIR.finditer(source)]
    if not pairs:
        raise RuntimeError("No debug functions found for behavior testing")
    fixtures = []
    for pair in pairs:
        save, load = re.findall(rf"function\s+({IDENTIFIER})\(", pair)
        fixtures.append({"source": pair, "save": save, "load": load})
    # Execute functions extracted from the real bundle, without activating the extension.
    program = """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
for (const fixture of JSON.parse(fs.readFileSync(0, 'utf8'))) {
  for (const initial of [undefined, '', 'my-app:*', 'release']) {
    const env = initial === undefined ? {} : {DEBUG: initial};
    const ctx = {process: {env}};
    const expected = initial ? {DEBUG: initial} : {};
    vm.runInNewContext(fixture.source, ctx);
    vm.runInNewContext(fixture.save + '(' + fixture.load + '())', ctx);
    assert.deepEqual(env, expected, 'debug initialization must not inject DEBUG=release');
    vm.runInNewContext(fixture.save + "('custom:*')", ctx);
    assert.deepEqual(env, {DEBUG: 'custom:*'}, 'save must preserve requested namespaces');
    vm.runInNewContext(fixture.save + "('')", ctx);
    assert.deepEqual(env, {}, 'disable must delete DEBUG');
  }
}
"""
    subprocess.run(["node", "-e", program], input=json.dumps(fixtures),
                   text=True, check=True, capture_output=True)
    print(f"Behavior passed: {len(pairs)} extracted debug pair(s)", flush=True)


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
                target.writestr(copy(info), data, compresslevel=1)
        # Reopen the delivered archive, not just the in-memory patch result.
        with zipfile.ZipFile(output) as delivered:
            assert delivered.namelist() == source.namelist(), "VSIX entry list changed"
            changed = []
            for info in source.infolist():
                actual = delivered.getinfo(info.filename)
                assert actual.external_attr == info.external_attr, "File permissions changed"
                if delivered.read(info.filename) != source.read(info):
                    changed.append(info.filename)
            assert changed == ([bundle_path] if count else []), f"Unexpected changed entries: {changed}"
            validate_debug_behavior(delivered.read(bundle_path).decode("utf-8"))
        print("Archive passed: only the extension bundle changed; manifests and binaries preserved", flush=True)
    report = {"asset": output.name, "patched_pairs": count, "source": asset["source"]}
    output.with_suffix(".json").write_text(json.dumps(report), encoding="utf-8")
    original.unlink()
    print(f"Validated {output.name}: {count} debug pair(s) patched", flush=True)
    return output, count, asset["source"]


def existing_release(repo, tag):
    # gh release view also finds drafts; the REST by-tag endpoint misses them.
    result = subprocess.run(["gh", "release", "view", tag, "--repo", repo,
                             "--json", "isDraft,assets"], text=True, capture_output=True)
    if result.returncode == 0:
        release = json.loads(result.stdout)
        return {"draft": release["isDraft"], "assets": release["assets"]}
    if "release not found" in result.stderr.lower() or "HTTP 404" in result.stderr:
        return None
    raise RuntimeError(result.stderr)


def asset_name(entry):
    platform = entry.get("targetPlatform") or "universal"
    return f"openai.chatgpt-{entry['version']}-{platform}-environment-variable-patch.vsix"


def prepare(entries, repo, path):
    previous = existing_release(repo, f"{entries[0]['version']}-environment-variable-patch")
    names = {a["name"] for a in previous["assets"]} if previous else set()
    skip = bool(previous and not previous["draft"]
                and {asset_name(e) for e in entries} <= names)
    plan = {"entries": entries, "skip": skip}
    path.write_text(json.dumps(plan), encoding="utf-8")
    matrix = {"platform": [e.get("targetPlatform") or "universal" for e in entries]}
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"skip={str(skip).lower()}\n")
            output.write(f"matrix={json.dumps(matrix)}\n")
    print(f"Plan: {entries[0]['version']}, {len(entries)} platforms, skip={skip}")


def publish(entries, repo, directory):
    version = entries[0]["version"]
    tag = f"{version}-environment-variable-patch"
    expected = {asset_name(e) for e in entries}
    files = {p.name: p for p in directory.glob("*.vsix")}
    if set(files) != expected:
        raise RuntimeError(f"Platform artifacts do not match plan: {set(files) ^ expected}")
    reports = [json.loads(p.with_suffix(".json").read_text(encoding="utf-8")) for p in files.values()]
    if {r["asset"] for r in reports} != expected:
        raise RuntimeError("Validation reports do not match platform artifacts")
    previous = existing_release(repo, tag)
    names = {a["name"] for a in previous["assets"]} if previous else set()
    notes = (
        f"Unofficial Codex VS Code extension {version} — environment variable patch.\n\n"
        "Fixes bundled debug save/load functions that inject DEBUG=release into the shared "
        "extension host. Extension identity and internal version are unchanged. "
        "No terminal environment settings are modified.\n\n"
        "Every platform passed JavaScript syntax checking, behavior tests of debug functions extracted "
        "from the delivered VSIX, and archive comparison confirming that only the bundle changed "
        "and manifests/binaries/permissions were preserved. This is not a full VS Code activation test.\n\n"
        "Install the VSIX matching your extension host OS/architecture via Extensions: Install from VSIX, "
        "then restart VS Code. Disable automatic updates for this extension to avoid Marketplace "
        "replacing the patched bundle. These modified packages are not signed by OpenAI.\n\n"
        "Upstream issue: https://github.com/openai/codex/issues/13694\n\n"
        "Validated assets:\n" + "\n".join(
            f"- {r['asset']}: {r['patched_pairs']} debug pair(s) patched; source: {r['source']}"
            for r in sorted(reports, key=lambda r: r["asset"])))
    if not previous:
        args = ["release", "create", tag, "--repo", repo, "--draft",
                "--title", f"{version} environment variable patch", "--notes", notes]
        if prerelease(entries[0]):
            args.append("--prerelease")
        gh(*args)
    missing = [str(files[name]) for name in sorted(expected - names)]
    if missing:
        gh("release", "upload", tag, *missing, "--repo", repo)
    if not previous or previous["draft"]:
        gh("release", "edit", tag, "--repo", repo, "--notes", notes, "--draft=false")
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
    # Prove that the behavioral test catches the original defect, not just syntax errors.
    try:
        validate_debug_behavior(source)
    except subprocess.CalledProcessError:
        print("Negative control passed: original DEBUG pollution is detected")
    else:
        raise AssertionError("Behavior test did not detect the original defect")
    validate_debug_behavior(patched)
    print("Self-test passed: defect detection, scoped matching and idempotence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="", help="Exact upstream version; blank selects latest")
    parser.add_argument("--channel", choices=["stable", "prerelease"], default="stable")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", type=Path, help="Resolve version and write a pinned platform plan")
    mode.add_argument("--build-only", action="store_true", help="Build and test without publishing")
    mode.add_argument("--publish-only", action="store_true", help="Publish collected platform artifacts")
    mode.add_argument("--self-test", action="store_true")
    parser.add_argument("--plan-file", type=Path, help="Use the pinned plan, without querying Marketplace again")
    parser.add_argument("--platform", help="Build-only platform filter")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if (args.prepare or args.publish_only) and not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    if args.platform and not args.build_only:
        parser.error("--platform is only supported with --build-only")
    if args.publish_only and not args.plan_file:
        parser.error("--publish-only requires --plan-file")
    entries = (json.loads(args.plan_file.read_text(encoding="utf-8"))["entries"] if args.plan_file
               else select_versions(marketplace_versions(), args.version, args.channel))
    if args.prepare:
        prepare(entries, args.repo, args.prepare)
        return
    if args.platform:
        entries = [e for e in entries if (e.get("targetPlatform") or "universal") == args.platform]
        if not entries:
            parser.error("Selected version does not have that platform")
    print(f"Selected {entries[0]['version']}: {len(entries)} platform(s)", flush=True)
    if args.build_only:
        args.output.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            patch_vsix(entry, args.output)
    else:
        publish(entries, args.repo, args.output)


if __name__ == "__main__":
    main()
