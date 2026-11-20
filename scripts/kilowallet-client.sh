#!/bin/bash
# Builds the client that Kilowallet embeds in its ROBLO tab: roblo-client-<tag>.zip and its
# SHA-256. Kilowallet pins that hash in roblo-client/CLIENT and checks it on every build.
#
#   scripts/kilowallet-client.sh <tag> [outdir]
#
# Reproducible: same commit + same frontend/package-lock.json => same zip, byte for byte. The
# webpack output is deterministic (content hashes), and the zip is written with its entries
# sorted, a fixed date and fixed permissions, so neither file order nor mtimes leak in.
# Needs frontend/node_modules installed from package-lock.json (npm ci).
set -euo pipefail
TAG=${1:?usage: $0 <tag> [outdir]}
OUT=${2:-$PWD}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
COMMIT=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain -- frontend scripts)" ]; then
  echo "frontend/ or scripts/ has uncommitted changes: the zip would not match $COMMIT" >&2
  exit 1
fi
# Built from a copy of the commit in a fixed folder: webpack names the WebAssembly files after
# the modules' paths, so the same commit built from two different folders differed there.
BUILDROOT=${ROBLO_BUILDROOT:-/tmp/roblo-client-build}
NM="$ROOT/frontend/node_modules"
test -d "$NM" || { echo "install frontend/node_modules first (npm ci)" >&2; exit 1; }
rm -rf "$BUILDROOT"; mkdir -p "$BUILDROOT"
git archive "$COMMIT" frontend LICENSE | tar -x -C "$BUILDROOT"
ln -s "$(readlink -f "$NM")" "$BUILDROOT/frontend/node_modules"
cd "$BUILDROOT/frontend"
NODE_ENV=production npx webpack --config webpack.config.ts --mode production --env target=kilowallet >"$BUILDROOT/webpack.log" 2>&1 \
  || { tail -30 "$BUILDROOT/webpack.log" >&2; exit 1; }
DIR=build-kilowallet/roblo
cp ../LICENSE "$DIR/LICENSE"
printf 'repo=https://github.com/Kilombino/roblosats\ntag=%s\ncommit=%s\nbuild=scripts/kilowallet-client.sh %s\n' \
  "$TAG" "$COMMIT" "$TAG" > "$DIR/SOURCE"
ZIP="$OUT/roblo-client-$TAG.zip"
python3 -I - "$DIR" "$ZIP" <<'PY'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
files = []
for base, dirs, names in os.walk(src):
    dirs.sort()
    for n in names:
        files.append(os.path.relpath(os.path.join(base, n), src))
files.sort()
with zipfile.ZipFile(dst + '.tmp', 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for rel in files:
        info = zipfile.ZipInfo(rel.replace(os.sep, '/'), date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        info.create_system = 3
        with open(os.path.join(src, rel), 'rb') as f:
            z.writestr(info, f.read(), compresslevel=9)
os.replace(dst + '.tmp', dst)
PY
cd "$OUT"
rm -rf "$BUILDROOT"
sha256sum "roblo-client-$TAG.zip" | tee "roblo-client-$TAG.zip.sha256"
