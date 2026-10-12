#!/bin/bash

cd /app/src

# OFFLINE TESTS POLICY: do NOT install dependencies or access the network here.
# All runtime deps, tools, and build steps must be installed in the Dockerfile
# (build time, where the internet is available). This container runs the tests
# with no network access, so any live install here will fail.

export CI=true
export INTEGRATION_TEST_WORKERS=2

# Local Postgres stands in for the Testcontainers instance
pg_ctlcluster "$(ls /etc/postgresql)" main start

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "packages/control-plane/src/authorization"
cp "/tests/packages/control-plane/src/authorization/policy.test.ts" "packages/control-plane/src/authorization/policy.test.ts"
mkdir -p "packages/control-plane/test/integration"
cp "/tests/packages/control-plane/test/integration/session-visibility.route.test.ts" "packages/control-plane/test/integration/session-visibility.route.test.ts"

cd packages/control-plane
# The two files belong to different Vitest projects, which cannot share one run
pnpm exec vitest run --config vitest.local.config.ts --project unit src/authorization/policy.test.ts
unit_status=$?
pnpm exec vitest run --config vitest.local.config.ts --project integration test/integration/session-visibility.route.test.ts
int_status=$?
test_status=$(( unit_status || int_status ))

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
