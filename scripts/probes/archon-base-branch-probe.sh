#!/usr/bin/env bash
# Archon base-branch and worktree probe for Factory Core Stage 5 (bash-only; no coding agent).
#
# NOT run by the test suite or by Factory Core. A human runs it, once per repository, after reading
# docs/probes/archon-probes.md and authorizing it explicitly. It refuses to start unless BOTH:
#   - FACTORY_ARCHON_PROBE_AUTHORIZED is set to the exact checkout path, and
#   - --i-have-read-docs-probes-archon-probes-md is passed.
#
# What it does in the given checkout:
#   1. checks the installed Archon version and that the checkout is clean;
#   2. writes ONE untracked workflow file, .archon/workflows/<probe>.yaml, containing only bash nodes;
#   3. runs `archon workflow run <probe> --branch <unique probe branch> --base <integration>`;
#   4. removes the workflow file again (also on failure).
# The bash nodes only read: branch, merge base against the origin/* refs as last fetched (run
# `git fetch` yourself first if they are stale), $BASE_BRANCH, remote branches. Nothing is pushed,
# merged or deleted. Archon creates a worktree and a local probe branch; both are left in place and
# the cleanup command is printed. Archon records the run in its own database (~/.archon).
set -euo pipefail

usage() {
  echo "usage: FACTORY_ARCHON_PROBE_AUTHORIZED=<checkout> $0 --i-have-read-docs-probes-archon-probes-md" \
    "--checkout <dir> --integration <branch> --release <branch> --expected-version <x.y.z>" >&2
  exit 64
}

ack="" checkout="" integration="" release="" expected=""
while [ $# -gt 0 ]; do
  case "$1" in
    --i-have-read-docs-probes-archon-probes-md) ack=1; shift ;;
    --checkout) checkout="${2:-}"; shift 2 ;;
    --integration) integration="${2:-}"; shift 2 ;;
    --release) release="${2:-}"; shift 2 ;;
    --expected-version) expected="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$ack" ] && [ -n "$checkout" ] && [ -n "$integration" ] && [ -n "$release" ] && [ -n "$expected" ] || usage

checkout="$(cd "$checkout" && pwd -P)"
if [ "${FACTORY_ARCHON_PROBE_AUTHORIZED:-}" != "$checkout" ]; then
  echo "refused: set FACTORY_ARCHON_PROBE_AUTHORIZED=$checkout to authorize a probe in this checkout" >&2
  exit 2
fi
branch_re='^[A-Za-z0-9._/-]{1,200}$'
for b in "$integration" "$release"; do
  if ! [[ "$b" =~ $branch_re ]] || [[ "$b" == -* ]]; then echo "refused: unsafe branch name: $b" >&2; exit 2; fi
done
if [ "$integration" = "$release" ]; then echo "refused: integration and release must differ" >&2; exit 2; fi

cd "$checkout"
installed="$(archon --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
if [ "$installed" != "$expected" ]; then
  echo "refused: archon reports '${installed:-nothing}', expected $expected" >&2; exit 2
fi
if [ -n "$(git status --porcelain)" ]; then echo "refused: the checkout has uncommitted changes" >&2; exit 2; fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
probe="factory-probe-$stamp"
probe_branch="factory-probe/$stamp"
workflow_file=".archon/workflows/$probe.yaml"
mkdir -p .archon/workflows
if [ -e "$workflow_file" ]; then echo "refused: $workflow_file exists" >&2; exit 2; fi
trap 'rm -f "$checkout/$workflow_file"; rmdir "$checkout/.archon/workflows" 2>/dev/null || true' EXIT

cat > "$workflow_file" <<YAML
name: $probe
description: Factory Core Stage 5 probe. Bash only; reads git state and prints it. No AI, no push.
nodes:
  - id: report
    bash: |
      set -eu
      echo "PROBE branch=\$(git rev-parse --abbrev-ref HEAD)"
      echo "PROBE base_branch_variable=\${BASE_BRANCH:-<unset>}"
      if git merge-base --is-ancestor "origin/$integration" HEAD; then echo "PROBE cut_from_integration=yes"; else echo "PROBE cut_from_integration=no"; fi
      if git merge-base --is-ancestor "origin/$release" HEAD && ! git merge-base --is-ancestor "origin/$integration" "origin/$release"; then echo "PROBE contains_release_only_commits=yes"; else echo "PROBE contains_release_only_commits=no"; fi
      echo "PROBE remote_has_probe_branch=\$(git ls-remote --heads origin "$probe_branch" | wc -l | tr -d ' ')"
      echo "PROBE worktree=\$(pwd -P)"
YAML

echo "probe: running $probe on $probe_branch with --base $integration in $checkout"
DO_NOT_TRACK=1 archon workflow run "$probe" --branch "$probe_branch" --base "$integration" "Factory Core probe" | tee "/tmp/$probe.log"

echo
echo "Expected: branch=$probe_branch, base_branch_variable=$integration, cut_from_integration=yes,"
echo "          contains_release_only_commits=no, remote_has_probe_branch=0, worktree outside $checkout."
echo "Record the PROBE lines from /tmp/$probe.log in docs/probes/archon-probes.md."
echo "Cleanup is manual: archon complete $probe_branch"
