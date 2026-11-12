# Archon probes: procedure only (execution disabled)

**Execution is disabled.** Factory Core ships no executable probe, so the Stage 4B §10 item "live
bash-only probe in each repo: worktree from the integration branch, nothing pushed" is not met: this
acceptance item remains incomplete for Stage 5. This document records why, what a future probe must
establish, and the conditions under which a human could run one by hand.

## Why the executable probe was removed

The second security review of PR #5 (finding R3) showed that the former script's guard recognized
hook configuration by matching text. Valid YAML spellings (flow mappings, quoted keys, JSON syntax,
anchors with merge keys, complex keys) bypassed it, so a repository's `worktree.postCreate` could run
when Archon created the probe's worktree. Hardening the guard was assessed and rejected:

- A guard would have to validate Archon's **effective** configuration, which includes layers held in
  Archon's database (per-user AI preferences, per-codebase settings) that Factory Core cannot read.
- An inspected file can change between inspection and the moment Archon reads it; nothing outside
  Archon can bind Archon's read to the inspected bytes.
- Even a bash-only workflow runs the Archon binary, which resolves the configured AI provider at run
  start and applies repository hooks when it creates the worktree. A reliable execution boundary
  could not be established, so the brief's fallback applies: no execution.

Factory Core keeps the useful part as a non-executing check. `factory preflight` parses the
repository's `.archon/config.yaml` structurally (one plain mapping, unique keys, no anchors, aliases,
merge keys or explicit tags) and allows only `worktree.baseBranch`, `assistant`, `tiers` and
`aliases`; hooks fail as `ARCHON_REPO_HOOK_PRESENT`, any other key as
`ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED`. The local file's digest is recorded and re-checked before the
report is returned.

## What a probe would have to establish

| Expectation | Why Factory Core depends on it |
|---|---|
| `--base <integration>` cuts the worktree from `origin/<integration>` | Work must start from the integration branch, never the release branch |
| `$BASE_BRANCH` equals the integration branch | It is Archon's PR target |
| The run is on the branch named by `--branch` | The branch name is the run key (task id + revision) |
| The run message is one argument; option-like text inside it is not parsed | The builder relies on it |
| Nothing is pushed | A probe must have no remote effect |
| The effective provider of a run is the expected one | Provider selection is not verified (see the provider section of [archon-adapter.md](../architecture/archon-adapter.md)) |

The first two were observed manually in the pilot workspace before Stage 5
(`docs/orchestration/archon-repo-config.md` there); they have not been re-observed for this adapter's
exact command.

## Conditions for a future manual probe (needs separate approval)

A human may decide to run a probe by hand outside Factory Core. Doing so executes the Archon binary
and is never part of the dry-run interface. Minimum conditions:

1. Separate approval for that run, recorded below.
2. Run as the agent OS user from 4B P2, with no Claude Code or Codex login and no provider API keys
   in the environment, so an unexpected provider call cannot succeed.
3. A disposable clone, registered as its own Archon project, whose `.archon/config.yaml` passes
   `factory preflight`'s structured check and has no workflow definitions besides the probe's.
4. A probe workflow with bash nodes only, written for that run and reviewed.
5. No coding workflow is invoked in any form, including flags meant to make it inert.

## Results

| Date | Repository | Archon | Probe | Result | Approved by |
|---|---|---|---|---|---|
| – | – | – | – | not run (execution disabled) | – |
