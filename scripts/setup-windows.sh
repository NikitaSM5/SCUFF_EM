#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
export PATH=/ucrt64/bin:/usr/bin
MAKE=(make CPPFLAGS=-D_USE_MATH_DEFINES)
mkdir -p build/logs library test/bin antenna/bin scripts/patches/windows
exec > >(tee -a build/logs/setup-windows.log) 2>&1
set -x
SHA=9c6d0cb7695463af803dee8d04cdae939740cdcc
SRC="$ROOT/library/source"
if [ ! -d "$SRC/.git" ]; then
  git -c core.autocrlf=false -c http.version=HTTP/1.1 clone --filter=blob:none https://github.com/HomerReid/scuff-em.git "$SRC"
  git -C "$SRC" -c core.autocrlf=false checkout "$SHA"
fi
git -C "$SRC" remote set-url origin https://github.com/HomerReid/scuff-em.git
git -C "$SRC" config core.autocrlf false
test "$(git -C "$SRC" rev-parse HEAD)" = "$SHA"
printf '%s\n' "$SHA" > build/scuff_commit.txt
pacman -Q > build/logs/windows-packages.txt
g++ --version
g++ -dumpmachine
for patch in "$ROOT"/scripts/patches/windows/*.patch; do
  [ -e "$patch" ] || continue
  if git -C "$SRC" apply --reverse --check "$patch" 2>/dev/null; then continue; fi
  git -C "$SRC" apply --check "$patch"
  git -C "$SRC" apply "$patch"
done
LIBRARIES=(libhrutil libhmat libSGJC libMDInterp libMatProp libTriInt libSpherical libIncField libSubstrate libStaticSolver libscuffSolver libscuff)
if [ ! -f "$SRC/Makefile" ] || [ "$(cat build/configured-root.txt 2>/dev/null || true)" != "$ROOT" ]; then
  (cd "$SRC"; bash autogen.sh --host=x86_64-w64-mingw32 --prefix="$ROOT/library" --without-python --without-hdf5 --disable-shared CC=gcc CXX=g++ F77=gfortran CXXFLAGS='-O2 -std=c++11' CFLAGS='-O2 -std=gnu11' BLAS_LIBS=-lopenblas LAPACK_LIBS=-lopenblas)
  # Libtool archives contain absolute paths; rebuild them after relocation.
  "${MAKE[@]}" -C "$SRC/libs/libMatProp/cmatheval" clean
  for library in "${LIBRARIES[@]}"; do
    "${MAKE[@]}" -C "$SRC/libs/$library" clean
  done
  "${MAKE[@]}" -C "$SRC/applications/scuff-rf" clean
  printf '%s\n' "$ROOT" > build/configured-root.txt
fi
# Build the library targets only; unrelated upstream test executables are not needed.
"${MAKE[@]}" -C "$SRC/libs/libMatProp/cmatheval" -j "${JOBS:-4}" libcmatheval.la
for library in "${LIBRARIES[@]}"; do
  "${MAKE[@]}" -C "$SRC/libs/$library" -j "${JOBS:-4}" "$library.la"
done
"${MAKE[@]}" -C "$SRC/applications/scuff-rf" -j "${JOBS:-4}"
"${MAKE[@]}" -C "$SRC/libs/libscuff" install-libLTLIBRARIES
for library in "${LIBRARIES[@]}"; do
  "${MAKE[@]}" -C "$SRC/libs/$library" install-pkgincludeHEADERS
done
"${MAKE[@]}" -C "$SRC/applications/scuff-rf" install
"${MAKE[@]}" -f scripts/helper.mk ROOT="$ROOT" SRC="$SRC" OUTPUT="$ROOT/test/bin/export_system.exe"
echo 'Native Windows compilation complete.'
