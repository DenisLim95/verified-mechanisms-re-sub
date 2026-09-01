#!/usr/bin/env bash
# Collect every dev8 artifact into one tarball at the repository root, so the
# pod can be destroyed as soon as the file is downloaded.
#
#   bash scripts/bundle_results.sh
#
# Safe to run at any time, including after an interrupted ladder.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_ROOT=${DEV8_OUT_ROOT:-outputs/dev8}
test -d "$OUT_ROOT" || { echo "nothing to bundle: $OUT_ROOT does not exist" >&2; exit 1; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
bundle="dev8-results-${stamp}.tar.gz"

# Regenerate the report so the bundle always carries current tables.
if [ -f "$OUT_ROOT/phase-ledger.tsv" ]; then
  .venv/bin/python scripts/phase_report.py "$OUT_ROOT/phase-ledger.tsv" \
    > "$OUT_ROOT/report.md" 2> "$OUT_ROOT/report.stderr" || true
fi

# Written at the repository root rather than inside the tree being archived,
# which would otherwise change while tar reads it.
tar czf "$bundle" "$OUT_ROOT" experiments.md

echo
echo "bundle: $(pwd)/$bundle  ($(du -h "$bundle" | cut -f1))"
echo
echo "Download it from your laptop, then the pod can be destroyed:"
echo "  scp root@<POD_IP>:$(pwd)/$bundle ."
echo "  tar xzf $bundle"
