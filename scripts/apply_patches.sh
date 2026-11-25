#!/usr/bin/env bash
set -euo pipefail

# Apply all Realme GT7 Pro (pista) patches onto a clean upstream Linux 7.3.6 tree.
# Usage: ./scripts/apply_patches.sh /path/to/linux-7.3.6

KSRC="${1:-}"
if [ -z "$KSRC" ] || [ ! -f "$KSRC/Makefile" ]; then
    echo "Usage: $0 <path-to-clean-linux-7.3.6-tree>" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

for p in "$REPO_DIR"/patches/*.patch; do
    echo "Applying $(basename "$p") ..."
    patch -d "$KSRC" -p1 --batch --forward < "$p"
done

echo "All patches applied cleanly to $KSRC."
