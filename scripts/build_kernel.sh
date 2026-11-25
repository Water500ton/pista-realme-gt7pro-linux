#!/usr/bin/env bash
set -euo pipefail

# Cross-compile mainline Linux 7.3.6 ARM64 kernel, DTB, and modules for Realme GT7 Pro (pista).
# Usage: ./scripts/build_kernel.sh /path/to/patched-linux-7.3.6 [out-dir]

KSRC="${1:-}"
OUT_DIR="${2:-$PWD/build}"
if [ -z "$KSRC" ] || [ ! -f "$KSRC/Makefile" ]; then
    echo "Usage: $0 <path-to-patched-linux-7.3.6> [build-dir]" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
KBUILD="$OUT_DIR/kbuild"
MOD_OUT="$OUT_DIR/modules"
JOBS="$(nproc)"

mkdir -p "$KBUILD" "$MOD_OUT"
cp "$REPO_DIR/configs/pista_defconfig" "$KSRC/arch/arm64/configs/pista_defconfig"

make -C "$KSRC" O="$KBUILD" ARCH=arm64 LLVM=1 pista_defconfig
make -C "$KSRC" O="$KBUILD" ARCH=arm64 LLVM=1 -j"$JOBS" Image qcom/sm8750-realme-pista.dtb modules
make -C "$KSRC" O="$KBUILD" ARCH=arm64 LLVM=1 INSTALL_MOD_PATH="$MOD_OUT" INSTALL_MOD_STRIP=1 modules_install

cp "$KBUILD/arch/arm64/boot/Image" "$OUT_DIR/Image.efi"
cp "$KBUILD/arch/arm64/boot/dts/qcom/sm8750-realme-pista.dtb" "$OUT_DIR/pista.dtb"

echo "Build complete:"
echo "  Kernel : $OUT_DIR/Image.efi"
echo "  DTB    : $OUT_DIR/pista.dtb"
echo "  Modules: $MOD_OUT/lib/modules/"
