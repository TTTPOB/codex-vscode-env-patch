#!/usr/bin/env bash
set -Eeuo pipefail

# Install a pre-patched VSIX and the latest independent NFS-fixed runtime.
EXTENSION_REPO=TTTPOB/codex-vscode-env-patch
FORK_REPO=TTTPOB/codex
UPSTREAM_REPO=openai/codex
CODE_CMD="${CODE_CMD:-code}"
LOCAL_BIN_DIR="${LOCAL_BIN_DIR:-$HOME/.local/bin}"
EXTENSION_VERSION="${EXTENSION_VERSION:-}"

usage() {
    printf '%s\n' \
        'Usage: update-vscode-codex-ext.sh [--version VERSION] [--help]' \
        'Download the patched linux-x64 VSIX from TTTPOB/codex-vscode-env-patch.' \
        'Without a version, use GitHub releases/latest. Never use Marketplace.' \
        'Runtime: latest stable nfs-rust-v* release; use its packaged host when available.' \
        'Overrides: EXTENSION_VERSION, CODE_CMD, LOCAL_BIN_DIR, EXTENSIONS_DIR,' \
        '           VSCODE_AGENT_FOLDER, TMPDIR, GH_TOKEN.' \
        'Reload the VS Code remote window after updating.'
}
die() { echo "ERROR: $*" >&2; exit 1; }
while (( $# )); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --version) (( $# >= 2 )) || die '--version needs a value'; EXTENSION_VERSION="$2"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ -z "$EXTENSION_VERSION" || "$EXTENSION_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'invalid extension version'
for cmd in curl python3 tar sha256sum install "$CODE_CMD"; do
    command -v "$cmd" >/dev/null || die "required command not found: $cmd"
done
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || die 'Linux x86_64 is required'

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/codex-vscode-update.XXXXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
api() {
    local args=(--fail --location --silent --show-error --retry 3
        --header 'Accept: application/vnd.github+json'
        --header 'X-GitHub-Api-Version: 2022-11-28')
    [[ -z "${GH_TOKEN:-}" ]] || args+=(--header "Authorization: Bearer $GH_TOKEN")
    curl "${args[@]}" "https://api.github.com/repos/$1"
}
download_verified() {
    local url="$1" digest="$2" dest="$3"
    [[ "$digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]] || die "missing GitHub SHA-256 digest: ${dest##*/}"
    curl --fail --location --silent --show-error --retry 5 --retry-delay 1 \
        --retry-all-errors "$url" --output "$dest"
    printf '%s  %s\n' "${digest#sha256:}" "$dest" | sha256sum --check --status || die "SHA-256 mismatch: ${dest##*/}"
}
asset() {
    python3 - "$1" "$2" <<'PY'
import json, sys
release = json.load(open(sys.argv[1]))
for asset in release.get('assets', []):
    if asset.get('name') == sys.argv[2] and asset.get('browser_download_url'):
        print(asset['browser_download_url'], asset.get('digest') or '', sep='\t')
        break
else:
    raise SystemExit(f"asset not found: {sys.argv[2]}")
PY
}
missing_extension() {
    echo 'Patched extension release/asset is unavailable; no official fallback.' >&2
    if [[ -n "$EXTENSION_VERSION" ]]; then
        echo "Generate it with: gh workflow run patch-release.yml --repo $EXTENSION_REPO -f version=$EXTENSION_VERSION" >&2
    fi
    exit 1
}

# Resolve and validate everything before changing the installed extension/runtime.
endpoint=latest
[[ -z "$EXTENSION_VERSION" ]] || endpoint="tags/${EXTENSION_VERSION}-environment-variable-patch"
api "$EXTENSION_REPO/releases/$endpoint" > "$TMP_DIR/extension-release.json" || missing_extension
EXT_VERSION="$(python3 - "$TMP_DIR/extension-release.json" "$EXTENSION_VERSION" <<'PY'
import json, re, sys
release = json.load(open(sys.argv[1]))
match = re.fullmatch(r'(\d+\.\d+\.\d+)-environment-variable-patch', release.get('tag_name', ''))
if not match or release.get('draft') or (sys.argv[2] and match[1] != sys.argv[2]):
    raise SystemExit('unexpected patched extension release tag')
print(match[1])
PY
)" || missing_extension
VSIX_ASSET="openai.chatgpt-${EXT_VERSION}-linux-x64-environment-variable-patch.vsix"
asset_info="$(asset "$TMP_DIR/extension-release.json" "$VSIX_ASSET")" || missing_extension
IFS=$'\t' read -r vsix_url vsix_digest <<< "$asset_info"
VSIX="$TMP_DIR/$VSIX_ASSET"
download_verified "$vsix_url" "$vsix_digest" "$VSIX"
python3 - "$VSIX" "$EXT_VERSION" <<'PY'
import json, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    package = json.loads(archive.read('extension/package.json'))
    if (package.get('publisher'), package.get('name'), package.get('version')) != ('openai', 'chatgpt', sys.argv[2]):
        raise SystemExit('VSIX package identity/version does not match release')
    prefix = 'extension/bin/linux-x86_64/'
    if prefix + 'codex' not in archive.namelist():
        raise SystemExit('VSIX does not contain the Linux x86_64 Codex bundle')
    bundled = 'unknown'
    try:
        bundled = json.loads(archive.read(prefix + 'codex-package.json')).get('version', 'unknown')
    except (KeyError, ValueError):
        pass
    print(f'Extension version: {package["version"]}; bundled Codex (information only): {bundled}')
PY

# Paginate so unrelated/prerelease entries cannot hide a usable stable release.
page=1
while :; do
    api "$FORK_REPO/releases?per_page=100&page=$page" > "$TMP_DIR/releases-$page.json"
    count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$TMP_DIR/releases-$page.json")"
    (( count == 100 )) || break
    page=$((page + 1))
done
FORK_TAG="$(python3 - "$TMP_DIR" <<'PY'
import glob, json, re, sys
releases = []
for path in glob.glob(sys.argv[1] + '/releases-*.json'):
    for release in json.load(open(path)):
        tag = release.get('tag_name', '')
        if release.get('draft') or release.get('prerelease') or not re.fullmatch(r'nfs-rust-v\d+\.\d+\.\d+', tag):
            continue
        wanted = f'codex-{tag}-x86_64-unknown-linux-musl.tar.gz'
        if any(a.get('name') == wanted for a in release.get('assets', [])):
            releases.append(release)
if not releases:
    raise SystemExit('no stable NFS-fixed release with a musl x64 archive')
latest = max(releases, key=lambda r: (r.get('published_at') or r.get('created_at') or '', tuple(map(int, r['tag_name'][10:].split('.')))))
print(latest['tag_name'])
with open(sys.argv[1] + '/fork-release.json', 'w') as output:
    json.dump(latest, output)
PY
)"
CODEX_VERSION="${FORK_TAG#nfs-rust-v}"
echo "NFS runtime: $FORK_TAG"
asset_info="$(asset "$TMP_DIR/fork-release.json" "codex-${FORK_TAG}-x86_64-unknown-linux-musl.tar.gz")"
IFS=
chmod 0755 "$FORK_BINARY" "$HOST_BINARY"
version_output="$("$FORK_BINARY" --version)"
[[ "$version_output" == "codex-cli $CODEX_VERSION" ]] || die "unexpected runtime version: $version_output"
"$HOST_BINARY" --help >/dev/null 2>&1 || die 'code-mode host smoke test failed'

REAL_CODEX="$LOCAL_BIN_DIR/codex"
RETRY_WRAPPER="$LOCAL_BIN_DIR/codex_"
{
    printf '#!/usr/bin/env bash\n# Retry fast NFS startup failures, not long-running crashes.\n'
    printf 'CODEX_BIN=%q\n' "$REAL_CODEX"
    printf '%s\n' 'attempt=1' 'while true; do' '    start=$SECONDS' \
        '    "$CODEX_BIN" "$@"' '    status=$?' \
        '    (( status != 0 )) || exit 0' \
        '    (( SECONDS - start < 30 && attempt < 10 )) || exit "$status"' \
        '    echo "codex startup failed ($status); retrying ($attempt/10)" >&2' \
        '    sleep 1' '    attempt=$((attempt + 1))' 'done'
} > "$TMP_DIR/wrapper"

code_cli() {
    local args=()
    [[ -z "${EXTENSIONS_DIR:-}" ]] || args+=(--extensions-dir "$EXTENSIONS_DIR")
    "$CODE_CMD" "${args[@]}" "$@"
}
installed_version() {
    code_cli --list-extensions --show-versions | awk -F@ 'tolower($1)=="openai.chatgpt" {print $2; exit}'
}
code_cli --install-extension "$VSIX" --force
[[ "$(installed_version)" == "$EXT_VERSION" ]] || die "installed extension version is not $EXT_VERSION"
EXT_DIR="$(python3 - "$EXT_VERSION" <<'PY'
import json, os, pathlib, sys
roots = [os.environ['EXTENSIONS_DIR']] if os.environ.get('EXTENSIONS_DIR') else []
if not roots:
    if os.environ.get('VSCODE_AGENT_FOLDER'):
        roots.append(os.environ['VSCODE_AGENT_FOLDER'] + '/extensions')
    roots += [os.path.expanduser(p) for p in ('~/.vscode-server/extensions', '~/.vscode-server-insiders/extensions', '~/.vscode/extensions')]
for root in roots:
    for candidate in sorted(pathlib.Path(root).glob('openai.chatgpt-*'), key=lambda p: not p.name.endswith('-linux-x64')):
        try:
            package = json.loads((candidate / 'package.json').read_text())
        except (OSError, ValueError):
            continue
        if (package.get('publisher'), package.get('name'), package.get('version')) == ('openai', 'chatgpt', sys.argv[1]):
            print(candidate.resolve())
            raise SystemExit(0)
raise SystemExit('installed extension directory not found')
PY
)"
BUNDLE_DIR="$EXT_DIR/bin/linux-x86_64"
[[ -e "$BUNDLE_DIR/codex" ]] || die 'installed extension has no bundled codex'
# Preserve the pristine extension binary on first replacement, including reruns.
if [[ ! -e "$BUNDLE_DIR/codex-orig" ]]; then
    [[ ! -L "$BUNDLE_DIR/codex" ]] || die 'codex is already a symlink without codex-orig'
    cp -p "$BUNDLE_DIR/codex" "$BUNDLE_DIR/codex-orig"
fi
atomic_install() {
    local source="$1" target="$2"
    install -m 0755 "$source" "${target}.new.$$"
    mv -Tf "${target}.new.$$" "$target"
}
mkdir -p "$LOCAL_BIN_DIR"
atomic_install "$FORK_BINARY" "$REAL_CODEX"
atomic_install "$HOST_BINARY" "$LOCAL_BIN_DIR/codex-code-mode-host"
atomic_install "$TMP_DIR/wrapper" "$RETRY_WRAPPER"
atomic_install "$HOST_BINARY" "$BUNDLE_DIR/codex-code-mode-host"
ln -s "$RETRY_WRAPPER" "$BUNDLE_DIR/.codex-link.$$"
mv -Tf "$BUNDLE_DIR/.codex-link.$$" "$BUNDLE_DIR/codex"
[[ "$(readlink "$BUNDLE_DIR/codex")" == "$RETRY_WRAPPER" && -x "$BUNDLE_DIR/codex" ]] || die 'invalid extension runtime symlink'
echo "Update complete: extension $EXT_VERSION; Codex $CODEX_VERSION."
echo "Extension: $EXT_DIR"
echo "Original Codex: $BUNDLE_DIR/codex-orig"
echo 'Reload the VS Code remote window before using the updated extension.'
\t' read -r fork_url fork_digest <<< "$asset_info"
download_verified "$fork_url" "$fork_digest" "$TMP_DIR/fork.tar.gz"
mkdir "$TMP_DIR/fork"
tar -xzf "$TMP_DIR/fork.tar.gz" -C "$TMP_DIR/fork"

if [[ -f "$TMP_DIR/fork/bin/codex" && -f "$TMP_DIR/fork/bin/codex-code-mode-host" ]]; then
    FORK_BINARY="$TMP_DIR/fork/bin/codex"
    HOST_BINARY="$TMP_DIR/fork/bin/codex-code-mode-host"
else
    # Backward compatibility with older fork releases that only shipped codex+bwrap.
    api "$UPSTREAM_REPO/releases/tags/rust-v$CODEX_VERSION" > "$TMP_DIR/host-release.json"
    asset_info="$(asset "$TMP_DIR/host-release.json" 'codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz')"
    IFS=
chmod 0755 "$FORK_BINARY" "$HOST_BINARY"
version_output="$("$FORK_BINARY" --version)"
[[ "$version_output" == "codex-cli $CODEX_VERSION" ]] || die "unexpected runtime version: $version_output"
"$HOST_BINARY" --help >/dev/null 2>&1 || die 'code-mode host smoke test failed'

REAL_CODEX="$LOCAL_BIN_DIR/codex"
RETRY_WRAPPER="$LOCAL_BIN_DIR/codex_"
{
    printf '#!/usr/bin/env bash\n# Retry fast NFS startup failures, not long-running crashes.\n'
    printf 'CODEX_BIN=%q\n' "$REAL_CODEX"
    printf '%s\n' 'attempt=1' 'while true; do' '    start=$SECONDS' \
        '    "$CODEX_BIN" "$@"' '    status=$?' \
        '    (( status != 0 )) || exit 0' \
        '    (( SECONDS - start < 30 && attempt < 10 )) || exit "$status"' \
        '    echo "codex startup failed ($status); retrying ($attempt/10)" >&2' \
        '    sleep 1' '    attempt=$((attempt + 1))' 'done'
} > "$TMP_DIR/wrapper"

code_cli() {
    local args=()
    [[ -z "${EXTENSIONS_DIR:-}" ]] || args+=(--extensions-dir "$EXTENSIONS_DIR")
    "$CODE_CMD" "${args[@]}" "$@"
}
installed_version() {
    code_cli --list-extensions --show-versions | awk -F@ 'tolower($1)=="openai.chatgpt" {print $2; exit}'
}
code_cli --install-extension "$VSIX" --force
[[ "$(installed_version)" == "$EXT_VERSION" ]] || die "installed extension version is not $EXT_VERSION"
EXT_DIR="$(python3 - "$EXT_VERSION" <<'PY'
import json, os, pathlib, sys
roots = [os.environ['EXTENSIONS_DIR']] if os.environ.get('EXTENSIONS_DIR') else []
if not roots:
    if os.environ.get('VSCODE_AGENT_FOLDER'):
        roots.append(os.environ['VSCODE_AGENT_FOLDER'] + '/extensions')
    roots += [os.path.expanduser(p) for p in ('~/.vscode-server/extensions', '~/.vscode-server-insiders/extensions', '~/.vscode/extensions')]
for root in roots:
    for candidate in sorted(pathlib.Path(root).glob('openai.chatgpt-*'), key=lambda p: not p.name.endswith('-linux-x64')):
        try:
            package = json.loads((candidate / 'package.json').read_text())
        except (OSError, ValueError):
            continue
        if (package.get('publisher'), package.get('name'), package.get('version')) == ('openai', 'chatgpt', sys.argv[1]):
            print(candidate.resolve())
            raise SystemExit(0)
raise SystemExit('installed extension directory not found')
PY
)"
BUNDLE_DIR="$EXT_DIR/bin/linux-x86_64"
[[ -e "$BUNDLE_DIR/codex" ]] || die 'installed extension has no bundled codex'
# Preserve the pristine extension binary on first replacement, including reruns.
if [[ ! -e "$BUNDLE_DIR/codex-orig" ]]; then
    [[ ! -L "$BUNDLE_DIR/codex" ]] || die 'codex is already a symlink without codex-orig'
    cp -p "$BUNDLE_DIR/codex" "$BUNDLE_DIR/codex-orig"
fi
atomic_install() {
    local source="$1" target="$2"
    install -m 0755 "$source" "${target}.new.$$"
    mv -Tf "${target}.new.$$" "$target"
}
mkdir -p "$LOCAL_BIN_DIR"
atomic_install "$FORK_BINARY" "$REAL_CODEX"
atomic_install "$HOST_BINARY" "$LOCAL_BIN_DIR/codex-code-mode-host"
atomic_install "$TMP_DIR/wrapper" "$RETRY_WRAPPER"
atomic_install "$HOST_BINARY" "$BUNDLE_DIR/codex-code-mode-host"
ln -s "$RETRY_WRAPPER" "$BUNDLE_DIR/.codex-link.$$"
mv -Tf "$BUNDLE_DIR/.codex-link.$$" "$BUNDLE_DIR/codex"
[[ "$(readlink "$BUNDLE_DIR/codex")" == "$RETRY_WRAPPER" && -x "$BUNDLE_DIR/codex" ]] || die 'invalid extension runtime symlink'
echo "Update complete: extension $EXT_VERSION; Codex $CODEX_VERSION."
echo "Extension: $EXT_DIR"
echo "Original Codex: $BUNDLE_DIR/codex-orig"
echo 'Reload the VS Code remote window before using the updated extension.'
\t' read -r host_url host_digest <<< "$asset_info"
    download_verified "$host_url" "$host_digest" "$TMP_DIR/host.tar.gz"
    mkdir "$TMP_DIR/host"
    tar -xzf "$TMP_DIR/host.tar.gz" -C "$TMP_DIR/host"
    FORK_BINARY="$TMP_DIR/fork/codex"
    HOST_BINARY="$(python3 - "$TMP_DIR/host" <<'PY'
import pathlib, sys
files = [p for p in pathlib.Path(sys.argv[1]).rglob('codex-code-mode-host*') if p.is_file() and not p.name.endswith('.sigstore')]
if len(files) != 1:
    raise SystemExit('expected one code-mode host binary')
print(files[0])
PY
)"
fi
[[ -f "$FORK_BINARY" ]] || die 'NFS archive has no codex binary'
chmod 0755 "$FORK_BINARY" "$HOST_BINARY"
version_output="$("$FORK_BINARY" --version)"
[[ "$version_output" == "codex-cli $CODEX_VERSION" ]] || die "unexpected runtime version: $version_output"
"$HOST_BINARY" --help >/dev/null 2>&1 || die 'code-mode host smoke test failed'

REAL_CODEX="$LOCAL_BIN_DIR/codex"
RETRY_WRAPPER="$LOCAL_BIN_DIR/codex_"
{
    printf '#!/usr/bin/env bash\n# Retry fast NFS startup failures, not long-running crashes.\n'
    printf 'CODEX_BIN=%q\n' "$REAL_CODEX"
    printf '%s\n' 'attempt=1' 'while true; do' '    start=$SECONDS' \
        '    "$CODEX_BIN" "$@"' '    status=$?' \
        '    (( status != 0 )) || exit 0' \
        '    (( SECONDS - start < 30 && attempt < 10 )) || exit "$status"' \
        '    echo "codex startup failed ($status); retrying ($attempt/10)" >&2' \
        '    sleep 1' '    attempt=$((attempt + 1))' 'done'
} > "$TMP_DIR/wrapper"

code_cli() {
    local args=()
    [[ -z "${EXTENSIONS_DIR:-}" ]] || args+=(--extensions-dir "$EXTENSIONS_DIR")
    "$CODE_CMD" "${args[@]}" "$@"
}
installed_version() {
    code_cli --list-extensions --show-versions | awk -F@ 'tolower($1)=="openai.chatgpt" {print $2; exit}'
}
code_cli --install-extension "$VSIX" --force
[[ "$(installed_version)" == "$EXT_VERSION" ]] || die "installed extension version is not $EXT_VERSION"
EXT_DIR="$(python3 - "$EXT_VERSION" <<'PY'
import json, os, pathlib, sys
roots = [os.environ['EXTENSIONS_DIR']] if os.environ.get('EXTENSIONS_DIR') else []
if not roots:
    if os.environ.get('VSCODE_AGENT_FOLDER'):
        roots.append(os.environ['VSCODE_AGENT_FOLDER'] + '/extensions')
    roots += [os.path.expanduser(p) for p in ('~/.vscode-server/extensions', '~/.vscode-server-insiders/extensions', '~/.vscode/extensions')]
for root in roots:
    for candidate in sorted(pathlib.Path(root).glob('openai.chatgpt-*'), key=lambda p: not p.name.endswith('-linux-x64')):
        try:
            package = json.loads((candidate / 'package.json').read_text())
        except (OSError, ValueError):
            continue
        if (package.get('publisher'), package.get('name'), package.get('version')) == ('openai', 'chatgpt', sys.argv[1]):
            print(candidate.resolve())
            raise SystemExit(0)
raise SystemExit('installed extension directory not found')
PY
)"
BUNDLE_DIR="$EXT_DIR/bin/linux-x86_64"
[[ -e "$BUNDLE_DIR/codex" ]] || die 'installed extension has no bundled codex'
# Preserve the pristine extension binary on first replacement, including reruns.
if [[ ! -e "$BUNDLE_DIR/codex-orig" ]]; then
    [[ ! -L "$BUNDLE_DIR/codex" ]] || die 'codex is already a symlink without codex-orig'
    cp -p "$BUNDLE_DIR/codex" "$BUNDLE_DIR/codex-orig"
fi
atomic_install() {
    local source="$1" target="$2"
    install -m 0755 "$source" "${target}.new.$$"
    mv -Tf "${target}.new.$$" "$target"
}
mkdir -p "$LOCAL_BIN_DIR"
atomic_install "$FORK_BINARY" "$REAL_CODEX"
atomic_install "$HOST_BINARY" "$LOCAL_BIN_DIR/codex-code-mode-host"
atomic_install "$TMP_DIR/wrapper" "$RETRY_WRAPPER"
atomic_install "$HOST_BINARY" "$BUNDLE_DIR/codex-code-mode-host"
ln -s "$RETRY_WRAPPER" "$BUNDLE_DIR/.codex-link.$$"
mv -Tf "$BUNDLE_DIR/.codex-link.$$" "$BUNDLE_DIR/codex"
[[ "$(readlink "$BUNDLE_DIR/codex")" == "$RETRY_WRAPPER" && -x "$BUNDLE_DIR/codex" ]] || die 'invalid extension runtime symlink'
echo "Update complete: extension $EXT_VERSION; Codex $CODEX_VERSION."
echo "Extension: $EXT_DIR"
echo "Original Codex: $BUNDLE_DIR/codex-orig"
echo 'Reload the VS Code remote window before using the updated extension.'
