FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Node.js is used by tests that execute extracted worker JS
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Fetch repo at HEAD commit (with fix applied).
# Shallow on purpose: only this one commit's worktree is needed, and .git is removed below.
RUN git init src && \
    cd src && \
    git remote add origin https://github.com/Ikalus1988/MisakaNet.git && \
    (git fetch --depth 1 origin 340224b2206e3a4e489f5dafd0a1fc3fb305b961 || git fetch --depth 1 origin "+refs/pull/3062/head:refs/remotes/origin/pr/3062") && \
    git checkout --detach FETCH_HEAD && \
    git submodule update --init --recursive

WORKDIR /app/src

ENV CI=true

RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python -r requirements.txt pytest
ENV PATH="/opt/venv/bin:${PATH}"

# If install/build steps touched tracked files, reset them so bug.patch applies cleanly,
RUN git reset --hard

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

WORKDIR /app/src
