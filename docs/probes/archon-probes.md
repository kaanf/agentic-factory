# Archon probes (opt-in, bash only)

Factory Core's command builder relies on Archon behavior that the test suite cannot verify offline.
These probes check it against the installed Archon, one repository at a time, **without a coding
agent**: the probe workflow contains only bash nodes. Nothing in Factory Core or its tests runs them.

**Status: prepared, not run.** Running a probe needs a separate, explicit human authorization. Record
results at the bottom of this file.

## What a probe changes

Read this before authorizing.

- Writes one untracked file, `.archon/workflows/factory-probe-<stamp>.yaml`, in the checkout, and
  removes it when the script exits (also on failure).
- Runs `archon workflow run` once. Archon creates a worktree and a **local** branch
  `factory-probe/<stamp>` and records the run in `~/.archon/archon.db`. Both stay until you remove
  them with `archon complete factory-probe/<stamp>`.
- Writes the run output to `/tmp/factory-probe-<stamp>.log`.
- Does **not** push, fetch, merge, open a PR, delete anything, or start Claude Code or Codex.
- Refuses to start if the checkout has uncommitted changes, if the installed Archon version is not the
  expected one, or if a branch argument is unsafe.

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

## Probe 2: provider selection (manual, no agent)

Archon 0.14.0's `workflow run` has no provider option, so the adapter refuses any provider other than
Archon's `defaultAssistant`. To learn whether `--model` or a `--config` layer could select a provider
without running an agent:

1. `archon workflow run archon-implement --dry-run --default-stubs "probe"` in a checkout. Archon
   documents `--dry-run` as simulating the workflow DAG "without creating a run or contacting a
   provider".
2. Repeat with `--model large=<a model of the other provider>` and note whether the dry-run output
   names the resolved provider.
3. If the output does not name the provider, per-run provider selection remains **unverified** and the
   adapter keeps refusing non-default providers (`PROVIDER_NOT_ARCHON_DEFAULT`).

Never run a non-dry-run `archon-implement` (or any workflow with prompt nodes) as a probe.

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
