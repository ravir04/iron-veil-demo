#!/usr/bin/env bash
# Iron-Veil Demo — Test Runner
#
# Usage:
#   bash tests/run_tests.sh           # unit tests only (no Signet required)
#   bash tests/run_tests.sh --all     # unit + integration (requires Signet running)
#   bash tests/run_tests.sh --gen     # generate sample FMV data only
#
# Run from the iron-veil-demo root directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT"

# Install test deps if needed
if ! python -c "import pytest" 2>/dev/null; then
  echo "[setup] Installing test dependencies..."
  pip install -r tests/requirements.txt -q
fi

if ! python -c "import cryptography" 2>/dev/null; then
  pip install -r services/drone-sim/requirements.txt -q
fi

MODE="${1:-}"

if [[ "$MODE" == "--gen" ]]; then
  echo "=== Generating synthetic FMV sample data ==="
  python tests/generate_sample_data.py
  exit 0
fi

echo "=== Unit tests (no Signet required) ==="
python -m pytest tests/test_klv_encoder.py tests/test_mission.py -v --tb=short
UNIT_RESULT=$?

if [[ "$MODE" == "--all" ]]; then
  echo ""
  echo "=== Integration tests (requires Signet at localhost:4774) ==="
  SIGNET_URL="${SIGNET_URL:-http://localhost:4774}"
  ISSUER_PRIVATE_KEY_PATH="${ISSUER_PRIVATE_KEY_PATH:-../iron-veil/deploy/config/trust/issuer_private.pem}"
  KAS_PUBLIC_KEY_PATH="${KAS_PUBLIC_KEY_PATH:-../iron-veil/deploy/config/trust/kas_public.pem}"
  export SIGNET_URL ISSUER_PRIVATE_KEY_PATH KAS_PUBLIC_KEY_PATH
  python -m pytest tests/test_pipeline.py -v --tb=short
  INTEG_RESULT=$?
else
  echo ""
  echo "(Skipping integration tests — run with --all to include them)"
  INTEG_RESULT=0
fi

echo ""
echo "=== Summary ==="
[[ $UNIT_RESULT -eq 0 ]]  && echo "  Unit tests:        PASSED" || echo "  Unit tests:        FAILED"
[[ "$MODE" == "--all" ]] && {
  [[ $INTEG_RESULT -eq 0 ]] && echo "  Integration tests: PASSED" || echo "  Integration tests: FAILED"
}

[[ $UNIT_RESULT -eq 0 && $INTEG_RESULT -eq 0 ]]
