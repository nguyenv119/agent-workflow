FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g corepack@latest && corepack enable && corepack prepare pnpm@10.28.0 --activate

WORKDIR /app

# Fetch repo at HEAD commit (with fix applied).
# Shallow on purpose: only this one commit's worktree is needed, and .git is removed below.
RUN git init src && \
    cd src && \
    git remote add origin https://github.com/chrisbenincasa/tunarr.git && \
    (git fetch --depth 1 origin 29dcdb92431b602504e2f2fcea58be11eed7807e || git fetch --depth 1 origin "+refs/pull/2230/head:refs/remotes/origin/pr/2230") && \
    git checkout --detach FETCH_HEAD && \
    git submodule update --init --recursive

WORKDIR /app/src

ENV CI=true
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0

RUN pnpm install --frozen-lockfile

# Build workspace dependencies of the server (types, shared) and generate env module
RUN pnpm turbo build --filter=@tunarr/types --filter=@tunarr/shared && \
    cd server && pnpm run generate-env

# If install/build steps touched tracked files, reset them so bug.patch applies cleanly,
RUN git reset --hard

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

WORKDIR /app/src
