FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv (manages the Python interpreter as well)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv

ENV UV_PYTHON=3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    CI=true

RUN uv python install 3.12

WORKDIR /app

# Fetch repo at HEAD commit (with fix applied).
# Shallow on purpose: only this one commit's worktree is needed, and .git is removed below.
RUN git init src && \
    cd src && \
    git remote add origin https://github.com/basicmachines-co/basic-memory.git && \
    (git fetch --depth 1 origin 6c2ad7bc445f6a7cbd8c5ba74bf9ed52f9f463d2 || git fetch --depth 1 origin "+refs/pull/1735/head:refs/remotes/origin/pr/1735") && \
    git checkout --detach FETCH_HEAD && \
    git submodule update --init --recursive

WORKDIR /app/src

# Install locked project + dev dependencies
RUN uv sync --locked --dev

ENV PATH="/opt/venv/bin:${PATH}"

# If install/build steps touched tracked files, reset them so bug.patch applies cleanly,
RUN git reset --hard

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

WORKDIR /app/src
