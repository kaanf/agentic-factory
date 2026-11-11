#!/usr/bin/env bash
# Archon base-branch and worktree probe for Factory Core Stage 5. MANUAL AND OPT-IN.
#
# This script is NOT part of Factory Core's dry-run interface: no `factory` command runs it, the test
# suite only exercises its refusal paths, and it is disabled unless a human authorizes it. Unlike
# `factory preflight`, it DOES execute things. Read docs/probes/archon-probes.md first.
#
# What it can execute when authorized:
#   - `archon --version`, then `archon workflow run <probe>` once: the Archon binary, its worktree
#     creation, its run bookkeeping (~/.archon/archon.db) and whatever Archon does at run start,
#     including resolving (not prompting) the configured AI provider;
#   - the probe workflow's single bash node, which only reads git state and prints it;
#   - git, read-only (status, rev-parse, merge-base, ls-remote).
# What it refuses to run in: a checkout whose .archon/config.yaml sets worktree hooks or file copies
# (postCreate, copyFiles, initSubmodules), environment, assistant, tier or alias overrides, or that
# already has any workflow definitions; Archon would apply those to the probe run. It never runs a
# workflow with prompt nodes. "Bash-only" limits what the probe workflow does, not what Archon does.
#
# Gates (all required): FACTORY_ARCHON_PROBE_AUTHORIZED=<exact checkout path>, the flag
# --i-have-read-docs-probes-archon-probes-md, a clean checkout, the expected Archon version.
#
# Effects: one untracked .archon/workflows/<probe>.yaml (removed on exit), one local branch and
# worktree created by Archon (left in place; cleanup command printed), one Archon run record, and a
# log in /tmp. Nothing is pushed, fetched, merged or deleted.
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
if [ -n "$(git --no-optional-locks status --porcelain)" ]; then echo "refused: the checkout has uncommitted changes" >&2; exit 2; fi
if [ -f .archon/config.yaml ] && grep -Eq '^[[:space:]]*(postCreate|postCreateTimeout|copyFiles|initSubmodules|env|assistant|assistants|tiers|aliases|workflows|defaults|commands)[[:space:]]*:' .archon/config.yaml; then
  echo "refused: .archon/config.yaml sets hooks, environment or AI overrides that Archon would apply to the probe" >&2
  exit 2
fi
if [ -L .archon ] || [ -L .archon/workflows ] || { [ -d .archon/workflows ] && [ -n "$(ls -A .archon/workflows)" ]; }; then
  echo "refused: .archon/workflows already has definitions (or is a link); the probe must run alone" >&2
  exit 2
fi

# Archon runs only after every static refusal above has passed.
installed="$(archon --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
if [ "$installed" != "$expected" ]; then
  echo "refused: archon reports '${installed:-nothing}', expected $expected" >&2; exit 2
fi
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
