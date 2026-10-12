FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Node 24 (.nvmrc = 24.12.0) + PostgreSQL 16 (replaces Testcontainers' postgres:16-alpine offline)
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y nodejs python3 postgresql postgresql-client && \
    rm -rf /var/lib/apt/lists/*

# Trust local connections so the integration harness can connect as postgres without a password
RUN PGHBA=$(ls /etc/postgresql/*/main/pg_hba.conf) && \
    sed -i -E 's/(scram-sha-256|md5|peer)$/trust/' "$PGHBA"

RUN npm install -g corepack && corepack enable && corepack prepare pnpm@11.19.0 --activate

WORKDIR /app

# Fetch repo at HEAD commit (with fix applied).
# Shallow on purpose: only this one commit's worktree is needed, and .git is removed below.
RUN git init src && \
    cd src && \
    git remote add origin https://github.com/agentconnect-md/agentconnect.git && \
    (git fetch --depth 1 origin 986fb09bff6fdf7c42aa8e3c88e90cb363b4709c || git fetch --depth 1 origin "+refs/pull/2815/head:refs/remotes/origin/pr/2815") && \
    git checkout --detach FETCH_HEAD && \
    git submodule update --init --recursive

WORKDIR /app/src

ENV CI=true \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0

RUN pnpm install --frozen-lockfile --filter "@agentconnect.md/control-plane..." --network-concurrency=8 --child-concurrency=2

# If install/build steps touched tracked files, reset them so bug.patch applies cleanly,
RUN git reset --hard

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

# Generate Prisma client (gitignored); generate is WASM-based, so skip the unreachable native schema-engine download
RUN PRISMA_SCHEMA_ENGINE_BINARY=/bin/true pnpm --filter @agentconnect.md/control-plane prisma:generate && \
    test -d packages/control-plane/src/generated/prisma

# Local-Postgres global setup + vitest config (verifier has no Docker for Testcontainers)
COPY harness/global-setup.local.ts packages/control-plane/test/global-setup.local.ts
RUN cd packages/control-plane && \
    sed "s#./test/global-setup.ts#./test/global-setup.local.ts#" vitest.config.ts > vitest.local.config.ts && \
    grep -q global-setup.local vitest.local.config.ts

RUN rm -rf /app/src/.git

WORKDIR /app/src
