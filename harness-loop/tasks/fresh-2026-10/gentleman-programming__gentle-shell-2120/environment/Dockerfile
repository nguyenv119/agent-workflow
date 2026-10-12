FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Node.js 24 (matches CI) and pnpm 11.1.1 (packageManager)
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@11.1.1

WORKDIR /app

# Fetch repo at HEAD commit (with fix applied).
# Shallow on purpose: only this one commit's worktree is needed, and .git is removed below.
RUN git init src && \
    cd src && \
    git remote add origin https://github.com/Gentleman-Programming/gentle-shell.git && \
    (git fetch --depth 1 origin 1b20acf9a3689645d5a81ad991be4e52e3c17acd || git fetch --depth 1 origin "+refs/pull/2120/head:refs/remotes/origin/pr/2120") && \
    git checkout --detach FETCH_HEAD && \
    git submodule update --init --recursive

WORKDIR /app/src

ENV CI=true
# Skip postinstall download of the gentle-ai native binary
ENV GENTLE_PI_SKIP_GENTLE_AI_INSTALL=1

RUN pnpm install --frozen-lockfile

# Reset any tracked files touched by install so bug.patch applies cleanly
RUN git reset --hard

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

WORKDIR /app/src
