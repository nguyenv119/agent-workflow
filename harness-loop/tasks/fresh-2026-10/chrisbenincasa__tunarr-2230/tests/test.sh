#!/bin/bash

cd /app/src

# OFFLINE TESTS POLICY: do NOT install dependencies or access the network here.
# All runtime deps, tools, and build steps must be installed in the Dockerfile
# (build time, where the internet is available). This container runs the tests
# with no network access, so any live install here will fail.

export CI=true

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "server/src/types"
cp "/tests/server/src/types/ffmpeg.test.ts" "server/src/types/ffmpeg.test.ts"

cd server
pnpm exec vitest run src/types/ffmpeg.test.ts --typecheck.tsconfig tsconfig.test.json --coverage.enabled=false --reporter=verbose --silent=false
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
