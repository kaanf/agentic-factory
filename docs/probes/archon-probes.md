# Archon probes (manual, opt-in)

Factory Core's command builder relies on Archon behavior that the test suite cannot verify offline.
These probes check it against the installed Archon, one repository at a time. **They are not part of
Factory Core's dry-run interface**: no `factory` command runs them, the test suite exercises only
their refusal paths, and each run needs a separate, explicit human authorization.

**Status: prepared, not run.** Record results at the bottom of this file.

## What a probe executes

"Bash-only" describes the probe *workflow*, not the whole run. An authorized probe executes:

- the **Archon binary**: `archon --version`, then one `archon workflow run`, including worktree
  creation, run bookkeeping in `~/.archon/archon.db`, and whatever Archon does at run start. That
  includes resolving the configured AI provider (Archon composes it from its config files and its
  database); no prompt is sent because the workflow has no prompt node, but the probe cannot prove
  that Archon contacts no provider service at all;
- the probe workflow's single **bash node**, which only reads git state and prints it;
- **git**, read-only (`--no-optional-locks status`, `rev-parse`, `merge-base`, `ls-remote`).

The script refuses, before starting Archon at all, a checkout whose `.archon/config.yaml` sets worktree
hooks or copies (`postCreate`, `copyFiles`, `initSubmodules`), environment, assistant, tier, alias,
workflow, default or command settings, or that already has workflow definitions: Archon would apply
them to the probe run, and `postCreate` executes repository-defined commands.

## What a probe changes

- Writes one untracked file, `.archon/workflows/factory-probe-<stamp>.yaml`, in the checkout, and
  removes it when the script exits (also on failure).
- Archon creates a worktree and a **local** branch `factory-probe/<stamp>` and records the run in
  `~/.archon/archon.db`. Both stay until you remove them with `archon complete factory-probe/<stamp>`.
- Writes the run output to `/tmp/factory-probe-<stamp>.log`.
- Does **not** push, fetch, merge, open a PR or delete anything, and never runs a workflow with
  prompt nodes (Claude Code or Codex are not started by the workflow).

## Safer design (recommended before running any probe)

The residual risk is Archon's own behavior at run start. Reduce it by running probes where an
unexpected provider call cannot succeed and cannot reach your credentials:

1. Run as the separate agent OS user from Stage 6 prerequisite P2, with **no** Claude Code or Codex
   login and no provider API keys in the environment.
2. Use a disposable clone registered as its own Archon project, not the working checkout.
3. Inspect `~/.archon/config.yaml` and the clone's `.archon/config.yaml` first; the script checks the
   latter only.
4. Run under `DO_NOT_TRACK=1` (the script sets it) and with network egress restricted to GitHub if
   your environment allows it.

## Probe 1: base branch and worktree

Confirms the assumptions behind `--branch <feature> --base <integration>`:

| Expectation | Why Factory Core depends on it |
|---|---|
| The worktree is cut from `origin/<integration>` | Work must start from the integration branch, never the release branch |
| `$BASE_BRANCH` equals the integration branch | It is Archon's PR target |
| The run is on the branch named by `--branch` | The branch name is the run key (task id + revision) |
| The worktree is outside the checkout | Isolation |
| Nothing was pushed (`remote_has_probe_branch=0`) | Bash-only probes must have no remote effect |

```sh
# 1. Optional: refresh origin/* so the merge-base checks are current.
git -C <checkout> fetch origin

# 2. Authorize this one checkout explicitly and run.
FACTORY_ARCHON_PROBE_AUTHORIZED="$(cd <checkout> && pwd -P)" \
  scripts/probes/archon-base-branch-probe.sh --i-have-read-docs-probes-archon-probes-md \
  --checkout <checkout> --integration <integration-branch> --release <release-branch> \
  --expected-version <validated archon version>

# 3. Clean up when you have recorded the result.
archon complete factory-probe/<stamp>
```

## Probe 2: provider selection (not a run)

Archon 0.14.0's `workflow run` has no provider option, and the effective provider is composed from
the global `defaultAssistant`, a repository `assistant` (which overrides it), tier and `@alias`
bindings in both files, and per-user and per-codebase settings held in Archon's database. Factory
Core checks every file layer (`PROVIDER_OVERRIDDEN_BY_REPOSITORY`, `MODEL_BINDING_PROVIDER_MISMATCH`,
`WORKFLOW_OVERRIDE_PRESENT`, ...) and reports the database layers as unobserved
(`ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED`), so no preflight passes.

**Do not probe provider selection by running a coding workflow**, not even with `--dry-run`: the
command names the agent workflow and depends on a single flag to stay inert. Establishing the
database layers is a Stage 6 prerequisite and needs either a documented, read-only Archon interface
for the effective AI configuration of a run, or an Archon configuration in which per-user
preferences are provably disabled for the agent OS user.

## Probe 3: message argument

Confirms that a message that starts with `Task ` and contains option-like text is passed as the
message, not parsed as options. Run Probe 1 with the probe workflow's bash node also printing
`$ARGUMENTS` (or the variable Archon uses for the run message) and a message such as
`Task X revision 1: --base stable --detach`. Expected: the text appears verbatim and the run uses
`--base` from the command line.

## Results

| Date | Repository | Archon | Probe | Result | Recorded by |
|---|---|---|---|---|---|
| – | – | – | – | not run | – |
