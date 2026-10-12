#!/bin/bash

cd /app/src

# OFFLINE TESTS POLICY: do NOT install dependencies or access the network here.
# All runtime deps, tools, and build steps must be installed in the Dockerfile
# (build time, where the internet is available). This container runs the tests
# with no network access, so any live install here will fail.

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "tests"
cp "/tests/test_export_okf_frontmatter.py" "tests/test_export_okf_frontmatter.py"
mkdir -p "tests"
cp "/tests/test_sync_answered_questions.py" "tests/test_sync_answered_questions.py"
mkdir -p "tests"
cp "/tests/test_worker_evidence_kind_gate.py" "tests/test_worker_evidence_kind_gate.py"

source /opt/venv/bin/activate
python -m pytest -v -p no:cacheprovider tests/test_export_okf_frontmatter.py tests/test_sync_answered_questions.py tests/test_worker_evidence_kind_gate.py
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
