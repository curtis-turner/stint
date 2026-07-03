#!/usr/bin/env bash
# Run the test suite across every supported Python version locally.
# Mirrors the CI matrix in .github/workflows/ci.yml. uv fetches any
# interpreter that isn't already installed.
#
#   ./scripts/test-all.sh            # pytest on each version
#   ./scripts/test-all.sh -k writes  # pass extra args through to pytest
set -euo pipefail

VERSIONS=("3.10" "3.11" "3.12" "3.13" "3.14")

cd "$(dirname "$0")/.."

failed=()
for v in "${VERSIONS[@]}"; do
  echo "── Python ${v} ──────────────────────────────────────────────"
  if uv run --python "${v}" --all-extras --group dev pytest -q "$@"; then
    echo "✓ ${v} passed"
  else
    echo "✗ ${v} FAILED"
    failed+=("${v}")
  fi
  echo
done

if ((${#failed[@]})); then
  echo "FAILED on: ${failed[*]}"
  exit 1
fi
echo "All supported versions passed: ${VERSIONS[*]}"
