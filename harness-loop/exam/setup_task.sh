#!/usr/bin/env bash
# Prepare one exam task repo for a headless `/work <bead>` run, in three phases:
#
#   setup_task.sh tools                                                  (root, install)
#   setup_task.sh pin <dolt|bd>                                          (prints url sha256 member)
#   setup_task.sh repo <repo_dir> <harness_tarball> <test_command> <logs_dir>   (agent user, install)
#   setup_task.sh bead <repo_dir> <title> <instruction_file> <run_name> <logs_dir>  (agent user, run)
#
# tools: installs jq, git, python3, curl, tar (package manager) and pinned
#   dolt + bd (release tarballs, sha256-checked) when missing. Pins exist for
#   Linux x86_64 and aarch64 (Docker on Apple silicon runs arm64 containers).
# pin: prints one tool's pinned url, sha256 and tarball member for this
#   machine, or fails naming the tool and machine when there is no pinned build.
# repo: refuses `/` and a repo whose parent dir the agent user cannot write
#   (the coordinator makes sibling worktrees there); puts the task state on
#   `main` as `exam: base` (git init if needed; a detached HEAD is fine) with a
#   fixed neutral identity; `bd init` (its edits to AGENTS.md/.gitignore are
#   reverted); unpacks the harness (a tracked `.claude/` moves to
#   `.claude-repo/`, `AGENTS.md` to `AGENTS.repo.md`), writes CLAUDE.md's
#   Quality Gates row and commits it all as `exam: harness` (worktrees are made
#   off origin/main, so the harness must be committed); pushes main to a local
#   bare origin ($EXAM_ORIGIN, default /tmp/origin.git). Writes base.sha,
#   start.sha into <logs_dir>.
# bead: creates one bead from the instruction (only run() has it), writes
#   `.claude/EXAM_MODE` (git-excluded, so only the main root has it) and
#   bead.txt; prints the bead id as the last stdout line.
# Every phase is safe to rerun.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: setup_task.sh tools
       setup_task.sh pin <dolt|bd>
       setup_task.sh repo <repo_dir> <harness_tarball> <test_command> <logs_dir>
       setup_task.sh bead <repo_dir> <title> <instruction_file> <run_name> <logs_dir>
EOF
  exit 2
}
[ $# -ge 1 ] || usage
phase=$1; shift
case "$phase:$#" in tools:0|pin:1|repo:4|bead:5) ;; *) usage ;; esac
started=$SECONDS

# sha256 per release asset, as each upstream release publishes it (beads:
# checksums.txt; dolt: the asset digests on its GitHub release).
BD_VERSION=0.59.0
BD_SHA256_amd64=d4395c4d3ab6312c50f8c3db4eeb97a357aec6716354a2c9318de3f33350702f
BD_SHA256_arm64=3be77b876d71bb7dd6bf2bd556f3b797152f91e1da7e25db196566b3da48b59f
DOLT_VERSION=1.83.4
DOLT_SHA256_amd64=620fb5503ea4d1011ae731008c27fcc35bd8a1dad9746b8a0a054afe397e9ecb
DOLT_SHA256_arm64=1c55949f48d895c73348c3ad8cfd3605d9d4210eff70b58706f3f516281b225e
# Dependency dirs never committed when setup has to `git init` a plain dir.
PLAIN_DIR_EXCLUDES="node_modules/ .venv/ venv/ __pycache__/ target/ .gradle/"

log() { echo "setup_task $phase: $*" >&2; }
die() { log "$*"; exit 1; }
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }
exclude() { grep -qxF "$1" .git/info/exclude 2>/dev/null || echo "$1" >> .git/info/exclude; }
abspath() { echo "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; }
# The pinned build of one tool for this machine: "<url> <sha256> <tarball member>".
pin() {
  local name=$1 arch sha
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64|Linux-amd64) arch=amd64 ;;
    Linux-aarch64|Linux-arm64) arch=arm64 ;;
    *) die "$name: no pinned build for $(uname -sm) (pinned: Linux x86_64, aarch64)" ;;
  esac
  case "$name" in
    dolt) sha=DOLT_SHA256_$arch
          echo "https://github.com/dolthub/dolt/releases/download/v$DOLT_VERSION/dolt-linux-$arch.tar.gz" \
               "${!sha}" "dolt-linux-$arch/bin/dolt" ;;
    bd) sha=BD_SHA256_$arch
        echo "https://github.com/steveyegge/beads/releases/download/v$BD_VERSION/beads_${BD_VERSION}_linux_$arch.tar.gz" \
             "${!sha}" bd ;;
    *) die "no pinned build of $name" ;;
  esac
}
need_file() { [ -f "$1" ] || { echo "setup_task: no such file: $1" >&2; exit 2; }; }
unset BEADS_DIR BEADS_DB  # never let an inherited setting point bd at another database
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"

phase_tools() {
  local bin_dir=/usr/local/bin missing=() t
  [ -w "$bin_dir" ] || { bin_dir=$HOME/.local/bin; mkdir -p "$bin_dir"; }
  for t in jq git python3 curl tar; do command -v "$t" >/dev/null || missing+=("$t"); done
  if [ ${#missing[@]} -gt 0 ]; then
    log "installing ${missing[*]}"
    if command -v apt-get >/dev/null; then
      as_root apt-get update -qq
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        "${missing[@]}" ca-certificates >/dev/null
    elif command -v apk >/dev/null; then as_root apk add --no-cache "${missing[@]}" ca-certificates >/dev/null
    elif command -v dnf >/dev/null; then as_root dnf install -y -q "${missing[@]}" >/dev/null
    else die "no package manager to install ${missing[*]}"
    fi
  fi
  install_bin() {  # name version
    local name=$1 version=$2 p url sha member tmp
    # Capture first: `grep -q` exiting early would SIGPIPE the writer under pipefail.
    if command -v "$name" >/dev/null && [[ "$("$name" version 2>/dev/null)" == *"$version"* ]]; then return; fi
    p=$(pin "$name")
    read -r url sha member <<<"$p"
    log "installing $name $version"
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/t.tgz" "$url"
    echo "$sha  $tmp/t.tgz" | sha256sum -c - >/dev/null || die "$name: sha256 mismatch"
    tar -xzf "$tmp/t.tgz" -C "$tmp" "$member"
    install -m 0755 "$tmp/$member" "$bin_dir/$name"
    rm -rf "$tmp"
  }
  install_bin dolt "$DOLT_VERSION"
  install_bin bd "$BD_VERSION"
}

phase_pin() { pin "$1"; }

phase_repo() {
  local repo=$1 tarball=$2 test_command=$3 logs=$4 origin=${EXAM_ORIGIN:-/tmp/origin.git}
  need_file "$tarball"
  tarball=$(abspath "$tarball")
  mkdir -p "$logs"
  [ -d "$repo" ] || die "no such repo dir: $repo"
  cd "$repo"
  [ "$(pwd -P)" != / ] || die "the task workdir is /; refusing to git-init the whole filesystem"
  # The coordinator makes each bead's worktree next to the repo (<repo>-bd-<id>-<slug>).
  [ -w "$(dirname "$(pwd -P)")" ] || die "$(dirname "$(pwd -P)") is not writable by $(id -un); /work cannot make sibling worktrees there"

  if [ "$(git rev-parse --show-toplevel 2>/dev/null)" != "$(pwd -P)" ]; then
    log "no git repo at $repo; initialising one"
    git init -q
    for d in $PLAIN_DIR_EXCLUDES; do exclude "$d"; done
  fi
  git config user.name "Exam Bot"
  git config user.email "exam@example.invalid"
  git config commit.gpgsign false

  if [ "$(git log -1 --format=%s 2>/dev/null)" != "exam: harness" ]; then
    if git rev-parse -q --verify HEAD >/dev/null; then git checkout -q -B main
    else git symbolic-ref HEAD refs/heads/main
    fi
    if [ -n "$(git status --porcelain)" ] || ! git rev-parse -q --verify HEAD >/dev/null; then
      git add -A
      git commit -q --allow-empty -m "exam: base"
    fi
    git rev-parse HEAD > "$logs/base.sha"

    # beads before the harness, so the tree is clean to revert
    if [ ! -f .beads/metadata.json ]; then
      bd init --quiet --skip-hooks --setup-exclude -p exam >&2
      git checkout -q -- .   # bd init edits AGENTS.md/.gitignore; the base is committed
      git clean -fdq          # and adds them when absent (.beads/ is excluded, so kept)
    fi

    local had_claude="" pointers=() cell
    if [ -n "$(git ls-files CLAUDE.md)" ]; then
      had_claude=$(mktemp)
      cp -L CLAUDE.md "$had_claude" 2>/dev/null || : > "$had_claude"  # -L: CLAUDE.md is often a link to AGENTS.md
      git rm -q --cached CLAUDE.md
      rm -f CLAUDE.md
    fi
    if [ -n "$(git ls-files .claude)" ]; then git mv .claude .claude-repo; pointers+=(".claude-repo/"); fi
    if [ -n "$(git ls-files AGENTS.md)" ]; then git mv AGENTS.md AGENTS.repo.md; pointers+=("AGENTS.repo.md"); fi
    tar -xzf "$tarball"

    if [ -n "$test_command" ]; then cell="\`${test_command//|/\\|}\`"; else cell="none (no visible tests)"; fi
    {
      if [ -n "$had_claude" ]; then cat "$had_claude"; echo; rm -f "$had_claude"; fi
      echo "## Quality Gates"
      echo
      echo "| Area | Tests | Lint |"
      echo "|------|-------|------|"
      echo "| repo | $cell | — |"
      if [ ${#pointers[@]} -gt 0 ]; then
        echo
        echo "This repo's own agent notes moved aside for the exam harness: ${pointers[*]}"
      fi
    } > CLAUDE.md
    git add -A
    git add -f .claude AGENTS.md CLAUDE.md  # even if the repo's .gitignore hides them
    git commit -q -m "exam: harness"
  fi
  git rev-parse HEAD > "$logs/start.sha"

  [ -d "$origin" ] || git init -q --bare "$origin"
  if git remote get-url origin >/dev/null 2>&1; then git remote set-url origin "file://$origin"
  else git remote add origin "file://$origin"
  fi
  git push -q -f origin main
  git fetch -q origin
}

phase_bead() {
  local repo=$1 title=$2 instruction=$3 run_name=$4 logs=$5 bead ok try
  need_file "$instruction"
  instruction=$(abspath "$instruction")
  mkdir -p "$logs"
  cd "$repo"
  [ "$(git log -1 --format=%s 2>/dev/null)" = "exam: harness" ] || [ -s .git/exam-bead ] \
    || die "repo phase has not run in $repo"
  if [ -s .git/exam-bead ]; then
    bead=$(cat .git/exam-bead)
  else
    # bd auto-starts one dolt server per repo. Rarely (seen in the .4 toy run and
    # once in about 100 local setups) a call reaches a server without the database;
    # restarting the project's server fixes it, so retry with a restart.
    bead="" ok=""
    for try in 1 2 3; do
      [ -n "$bead" ] || bead=$(bd create "$title" --body-file "$instruction" --silent) || bead=""
      if [ -n "$bead" ] && bd show "$bead" >/dev/null; then ok=1; break; fi
      log "bd create/show failed (try $try); restarting this repo's dolt server"
      bd dolt stop >/dev/null 2>&1 || true
      sleep 2
    done
    [ -n "$ok" ] || die "bd create/show failed 3 times"
    echo "$bead" > .git/exam-bead
  fi
  echo "$bead" > "$logs/bead.txt"
  echo "$run_name" > .claude/EXAM_MODE
  exclude .claude/EXAM_MODE
  echo "$bead"
}

"phase_$phase" "$@"
log "done in $((SECONDS - started)) s"
