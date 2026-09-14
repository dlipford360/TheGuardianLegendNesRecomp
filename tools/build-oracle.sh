#!/usr/bin/env bash
set -euo pipefail

# Source pin, not a moving branch or buildbot binary. No ROM is needed to build.
readonly MESEN_REV=0102910c39ad1a62bc3f784466f3f67ca9eae335
readonly MESEN_URL=https://github.com/libretro/Mesen.git
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT
readonly SRC="$ROOT/build/oracle-src/Mesen"
readonly OUT="$ROOT/build/oracle"

if [[ "$(uname -s)" != Darwin || "$(uname -m)" != x86_64 ]]; then
    printf '%s\n' 'This core build is pinned and tested for Intel x86_64 macOS.' >&2
    exit 1
fi
for tool in git cmake make clang clang++ shasum; do
    command -v "$tool" >/dev/null || { printf 'Missing prerequisite: %s\n' "$tool" >&2; exit 1; }
done
# Parent project owns ignore rules. Do not accidentally expose third-party source.
git -C "$ROOT" check-ignore -q build/oracle-src/Mesen/ || {
    printf '%s\n' 'The parent repository must ignore /build/ before building the oracle.' >&2
    exit 1
}
mkdir -p "$ROOT/build/oracle-src" "$OUT"
if [[ ! -d "$SRC/.git" ]]; then
    git clone --no-checkout "$MESEN_URL" "$SRC"
fi
if [[ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]]; then
    printf '%s\n' 'Refusing to build modified Mesen source.' >&2
    exit 1
fi
git -C "$SRC" cat-file -e "$MESEN_REV^{commit}" 2>/dev/null || git -C "$SRC" fetch origin "$MESEN_REV"
git -C "$SRC" checkout --detach "$MESEN_REV"
test "$(git -C "$SRC" rev-parse HEAD)" = "$MESEN_REV"

# Upstream defaults to obsolete i386+x86_64; explicitly select Intel 64-bit.
# Clean avoids stale objects from other revisions, architectures or build flags.
make -C "$SRC/Libretro" clean platform=osx ARCHFLAGS='-arch x86_64'
make -C "$SRC/Libretro" -j"${JOBS:-4}" platform=osx ARCHFLAGS='-arch x86_64' CC=clang CXX=clang++ LTO=false
cmake -E copy_if_different "$SRC/Libretro/mesen_libretro.dylib" "$OUT/mesen_libretro.dylib"
cmake -S "$ROOT/tools/oracle" -B "$OUT" -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=x86_64
cmake --build "$OUT" --parallel "${JOBS:-4}"
{
    printf 'source=%s\nrevision=%s\narchitecture=x86_64\n' "$MESEN_URL" "$MESEN_REV"
    printf 'core_sha256='
    shasum -a 256 "$OUT/mesen_libretro.dylib"
    clang++ --version
} > "$OUT/core-build.txt"
printf '\nOracle: %s/nesref\nCore: %s/mesen_libretro.dylib\nProvenance: %s/core-build.txt\n' "$OUT" "$OUT" "$OUT"
