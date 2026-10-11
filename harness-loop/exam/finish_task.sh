#!/usr/bin/env bash
# After `/work` exits: put the bead's branch in the task workdir so the hidden
# tests grade what /work built, and export the try files.
#
#   finish_task.sh <repo_dir> <logs_dir>
#
# /work builds in a sibling worktree and pushes `feature/bd-<bead>-<slug>` to
# the exam's bare origin; the workdir Harbor's verifier grades is still at the
# `exam: harness` commit. This fetches origin and checks the bead branch out
# detached (a branch naming the bead, else the only non-main branch). With no
# such branch the workdir is reset to start (it will score 0).
# Writes end.sha, diff.patch (start..end, --binary), branch.txt (empty if none)
# and origin_url.txt into <logs_dir>, next to setup_task.sh's start.sha and
# bead.txt.
set -euo pipefail

[ $# -eq 2 ] || { echo "usage: finish_task.sh <repo_dir> <logs_dir>" >&2; exit 2; }
repo=$1 logs=$2
cd "$repo"
bead=$(cat "$logs/bead.txt")
start=$(cat "$logs/start.sha")

git fetch -q --prune origin
others=()
match=""
while read -r b; do
  case "$b" in main|HEAD) continue ;; esac
  others+=("$b")
  case "$b" in *bd-"$bead"-*|*bd-"$bead") [ -n "$match" ] || match=$b ;; esac
done < <(git for-each-ref --format='%(refname:lstrip=3)' refs/remotes/origin)
if [ -z "$match" ] && [ ${#others[@]} -eq 1 ]; then match=${others[0]}; fi

# -f and clean: stray edits and files in the main root are not the bead's work,
# so they must not reach the grader (no -x: .beads/ and EXAM_MODE are excluded
# and survive).
if [ -n "$match" ]; then
  git checkout -q -f --detach "origin/$match"
  echo "finish_task: graded tree = origin/$match" >&2
else
  git checkout -q -f --detach "$start"
  echo "finish_task: no bead branch on origin; workdir reset to start" >&2
fi
git clean -fdq
end=$(git rev-parse HEAD)

echo "$match" > "$logs/branch.txt"
echo "$end" > "$logs/end.sha"
git remote get-url origin > "$logs/origin_url.txt"
git diff --binary "$start" "$end" > "$logs/diff.patch"
