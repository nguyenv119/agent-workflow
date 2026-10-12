#!/bin/bash

cd /app/src

# OFFLINE TESTS POLICY: do NOT install dependencies or access the network here.
# All runtime deps, tools, and build steps must be installed in the Dockerfile
# (build time, where the internet is available). This container runs the tests
# with no network access, so any live install here will fail.

export CI=true

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "test-int"
cp "/tests/test-int/test_vector_materialization_order.py" "test-int/test_vector_materialization_order.py"
mkdir -p "tests/api/v2"
cp "/tests/api/v2/test_knowledge_router.py" "tests/api/v2/test_knowledge_router.py"
mkdir -p "tests/api/v2"
cp "/tests/api/v2/test_knowledge_router_telemetry.py" "tests/api/v2/test_knowledge_router_telemetry.py"
mkdir -p "tests/cloud"
cp "/tests/cloud/test_note_content_materialization.py" "tests/cloud/test_note_content_materialization.py"

/opt/venv/bin/python -m pytest -v -p no:cacheprovider --no-cov \
  test-int/test_vector_materialization_order.py \
  tests/api/v2/test_knowledge_router.py \
  tests/api/v2/test_knowledge_router_telemetry.py \
  tests/cloud/test_note_content_materialization.py
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
