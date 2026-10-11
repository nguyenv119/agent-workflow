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
# such branch the workdir stays at start (it will score 0).
# Writes end.sha, diff.patch (start..end) and branch.txt (empty if none) into
# <logs_dir>, next to setup_task.sh's start.sha and bead.txt.
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

if [ -n "$match" ]; then
  # -f: stray edits in the main root are not the bead's work; the branch is.
  git checkout -q -f --detach "origin/$match"
  echo "finish_task: graded tree = origin/$match" >&2
else
  echo "finish_task: no bead branch on origin; workdir stays at start" >&2
fi
end=$(git rev-parse HEAD)
[ -n "$match" ] || end=$start

echo "$match" > "$logs/branch.txt"
echo "$end" > "$logs/end.sha"
git diff "$start" "$end" > "$logs/diff.patch"
