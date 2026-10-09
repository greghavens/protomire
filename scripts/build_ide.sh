#!/usr/bin/env bash
# Source-based Linux x64 development distribution, no Code OSS core patches.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CACHE=${MODELBUILDER_BUILD_CACHE:-"$ROOT/.cache"}
SOURCE="$CACHE/code-oss"
LOG="$ROOT/evidence/gui/distribution"
MODE=${1:-build}
mkdir -p "$CACHE" "$LOG"
exec > >(tee "$LOG/build-ide.log") 2>&1
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo 'This build script currently supports Linux x64 only.'; exit 2; }
PIN="$ROOT/distribution/code_oss_pin.json"
readarray -t VALUES < <(python3 - "$PIN" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]));print(p['repository']);print(p['tag']);print(p['commit']);print(p['node_version']);print(p['node_linux_x64_sha256'])
PY
)
if [[ ! -d "$SOURCE/.git" ]]; then
  git clone --depth 1 --single-branch --branch "${VALUES[1]}" "${VALUES[0]}" "$SOURCE"
fi
[[ $(git -C "$SOURCE" rev-parse HEAD) == "${VALUES[2]}" ]] || { echo 'Upstream pin mismatch; use a clean build cache.'; exit 3; }
NODE_DIR="$CACHE/toolchains/node-v${VALUES[3]}-linux-x64"
if [[ ! -x "$NODE_DIR/bin/node" ]]; then
  mkdir -p "$CACHE/toolchains"
  curl --fail --location --retry 2 "https://nodejs.org/dist/v${VALUES[3]}/node-v${VALUES[3]}-linux-x64.tar.xz" -o "$CACHE/toolchains/node.tar.xz"
  printf '%s  %s\n' "${VALUES[4]}" "$CACHE/toolchains/node.tar.xz" | sha256sum --check -
  tar -xJf "$CACHE/toolchains/node.tar.xz" -C "$CACHE/toolchains"
fi
export PATH="$NODE_DIR/bin:$PATH"
export npm_config_cache="$CACHE/npm" npm_config_devdir="$CACHE/node-gyp" XDG_CACHE_HOME="$CACHE"
export npm_config_jobs=2
export NODE_OPTIONS=--max-old-space-size=6144
if [[ -d "$CACHE/sysroot/usr/lib/x86_64-linux-gnu/pkgconfig" ]]; then
  export PKG_CONFIG_SYSROOT_DIR="$CACHE/sysroot"
  export PKG_CONFIG_PATH="$CACHE/sysroot/usr/lib/x86_64-linux-gnu/pkgconfig:$CACHE/sysroot/usr/share/pkgconfig"
fi
mkdir -p "$npm_config_cache" "$npm_config_devdir"
[[ $(node --version) == "v${VALUES[3]}" ]] || exit 4
python3 "$ROOT/distribution/apply_overlay.py" "$SOURCE"
if [[ "$MODE" == prepare ]]; then exit 0; fi
if [[ "$MODE" != build && "$MODE" != compile && "$MODE" != package ]]; then echo 'Usage: build_ide.sh [prepare|build|compile|package]'; exit 2; fi
if [[ "$MODE" == build || "$MODE" == package ]]; then
  (cd "$SOURCE" && npm ci --no-audit --no-fund)
fi
(cd "$ROOT/packages/ide_feature" && npm ci --no-audit --no-fund && npm run compile)
# Build the upstream product before overlaying the already-compiled feature.
# This prevents upstream extension discovery from attempting to recompile it.
(cd "$SOURCE" && npm run compile)
if [[ -n ${HTTPS_PROXY:-} || -n ${HTTP_PROXY:-} ]]; then
  # The upstream Electron downloader needs its documented proxy bootstrap.
  # Retain the managed proxy; do not disable TLS or route around network policy.
  (cd "$SOURCE" && ELECTRON_GET_USE_PROXY=1 GLOBAL_AGENT_HTTP_PROXY="${HTTP_PROXY:-}" GLOBAL_AGENT_HTTPS_PROXY="${HTTPS_PROXY:-}" node -r global-agent/bootstrap build/lib/electron.js)
else
  (cd "$SOURCE" && npm run electron)
fi
python3 "$ROOT/distribution/apply_overlay.py" "$SOURCE" --feature "$ROOT/packages/ide_feature" | tee "$LOG/source-integrity.json"
mkdir -p "$ROOT/.runtime/bin"
cat > "$ROOT/.runtime/bin/modelbuilder-ide" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PATH="$NODE_DIR/bin:\$PATH"
export VSCODE_SKIP_PRELAUNCH=1
for arg in "\$@"; do
  case "\$arg" in
    --install-extension|--uninstall-extension|--list-extensions|--version)
      exec "$SOURCE/scripts/code-cli.sh" "\$@" ;;
  esac
done
exec "$SOURCE/scripts/code.sh" "\$@"
EOF
chmod +x "$ROOT/.runtime/bin/modelbuilder-ide"
if [[ "$MODE" == package ]]; then
  (cd "$SOURCE" && npm run gulp vscode-linux-x64-min)
fi
python3 "$ROOT/distribution/verify_build.py" "$SOURCE" | tee "$LOG/build-manifest.json"
echo "Built development distribution: $ROOT/.runtime/bin/modelbuilder-ide"
