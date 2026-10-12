#!/bin/bash

cd /app/src

# OFFLINE TESTS POLICY: do NOT install dependencies or access the network here.
# All runtime deps, tools, and build steps must be installed in the Dockerfile
# (build time, where the internet is available). This container runs the tests
# with no network access, so any live install here will fail.

export CI=true

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "tests"
cp "/tests/test_web_api_validate_config.py" "tests/test_web_api_validate_config.py"

/opt/venv/bin/python -m pytest -v -p no:cacheprovider tests/test_web_api_validate_config.py
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
