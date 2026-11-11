# 0006: Stage 5 security review remediation (F1-F6)

- Status: proposed (for a second independent security review on PR #5)
- Date: 2026-11-12
- Reviewed head: `4eb012d` (Stage 5 as first submitted). Baseline: `34ef0ae` on `dev`.
- Inputs: the independent security review of PR #5 (five Medium findings, one Low, no confirmed
  dispatch bypass); Stage 4B §7-§10; Archon 0.14.0's configuration code, read from the installed
  binary's strings without running it.

Every finding was reproduced on `4eb012d` before any change, with one script run unchanged against
both heads (see the PR description). Each fix is narrow; the Stage 5 architecture is unchanged.

| # | Severity | Finding | Root cause | Fix |
|---|---|---|---|---|
| F1 | Medium | Symbolic, malformed, symlinked or unreadable refs and worktree metadata were dropped or became INFO notes; preflight could PASS and reconcile return `NO_RUN` with an unresolved branch present | The reader returned only refs it could resolve, so "not in the dict" meant both "absent" and "could not tell" | `gitstate` keeps tri-state facts: resolved refs, *present but unresolved* refs (never followed), and *unknown* subtrees; worktrees carry `branch`/`detached`/`unknown`; HEAD may be unknown. `branch_state`, `worktree_state` and `remote_tracking_state` answer `present`/`absent`/`unknown`. Reconcile maps unknown to UNKNOWN and unresolved task branches (or symbolic refs pointing at one) to UNCERTAIN; preflight reports `FEATURE_BRANCH_LOCAL_UNKNOWN`, `FEATURE_WORKTREE_UNKNOWN`, `REMOTE_TRACKING_UNKNOWN`, `CHECKOUT_HEAD_UNKNOWN`, `LOCAL_REF_UNRESOLVED`. Unrelated broken refs stay notes |
| F2 | Medium | Malformed matching-ref entries and PR records were filtered silently; a missing `head.repo` counted as a fork | Relevance was decided before identity was validated | `collector.branch_pull_requests` skips a record only when it is confirmed to be another branch or another repository; any missing or malformed identity field raises (the listing is incomplete). `reconcile.remote_branch_names` validates every matching-ref entry and treats a listing of 100+ entries as incomplete; preflight uses the same helper. Stage 2 collection now omits such a branch's facts (UNKNOWN) instead of filtering or crashing |
| F3 | Medium | `ConfinedTree.open` resolved the root with `realpath`, then opened the resulting path; replacing an ancestor in between redirected the read | Resolve-then-open on a path | The root is acquired by one descriptor-relative walk from `/` (or the working directory): each component is opened relative to the previous descriptor; a symbolic link in the root's own path is read with `readlinkat` from the descriptor holding it and the walk continues; there is no `realpath`. The displayed root is the sequence of directories actually opened. Below the root nothing changed (`O_NOFOLLOW` per component). Platforms without directory-relative `open`, `stat` and `readlink` are refused |
| F4 | Medium | The provider check used only the global `defaultAssistant`; repository `assistant`, tier and alias bindings can change the effective provider | Archon's layered AI configuration was not modelled | `archon.check_effective_provider` applies Archon 0.14.0's file layers: a repository `assistant` overrides the global default, local and remote repository configs must agree, and every tier and `@alias` binding in either file must name the task's provider; unsupported binding forms are refused. `check_workflow_definitions` refuses a workflow file that could replace the reviewed `archon-implement`. Archon's database-held layers (per-user AI preferences, which outrank the repository assistant, and per-codebase settings) cannot be read, so `ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED` (UNKNOWN) is always reported and **no preflight passes** |
| F5 | Medium | Revision 1,000,000 materialized a million-entry branch map despite `MAX_HISTORY = 50` | `range(1, revision + 1)` built before the window applied | Only revisions found among the returned refs are stored (`frozenset`); presence is membership. Pull-request lookups and older-revision facts stay within the 51-revision window; beyond it `HISTORY_TRUNCATED` keeps the result UNKNOWN, as before |
| F6 | Low | `factory preflight --live` collected evidence (GitHub requests) before checking the dispatch lock | The CLI ran `plan_live` before calling `preflight()` | The CLI probes the lock first and, when held, prints `preflight.lock_refusal` and exits 3 with no request and no snapshot read. Documented as a Stage 5 observation, not Stage 6 exclusive locking; it does not remove TOCTOU |

## Probe policy (review note on `scripts/probes/archon-base-branch-probe.sh`)

The script is a manual execution path, not part of the dry-run interface (no `factory` command
references it; tested). Changes:

- Its header and `docs/probes/archon-probes.md` state exactly what it executes: the Archon binary
  (`--version`, one `workflow run` with worktree creation and run-start provider resolution), one bash
  node that reads git state, and read-only git. "Bash-only" is no longer used as a safety claim.
- It refuses, before starting Archon, a checkout whose `.archon/config.yaml` sets hooks or copies
  (`postCreate`, `copyFiles`, `initSubmodules`), environment, assistant, tier, alias, workflow,
  default or command settings, or that already has workflow definitions.
- Its clean-tree check uses `git --no-optional-locks status`, which does not refresh the index.
- The former Probe 2 (`archon workflow run archon-implement --dry-run`) is removed: it named the
  coding workflow and depended on one flag to stay inert. Recommended safer design: run probes as the
  P2 agent OS user without provider logins or keys, in a disposable clone.

## Behavior changes

- `factory preflight` can no longer exit 0 (see F4). Two Stage 5 tests that asserted a passing dry
  run now assert the stricter result: every observable check passes, the only non-PASS check is
  `ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED`, and authorization is false even for a report forced
  to PASS.
- `evidence collect` and `plan --live`: a branch whose PR listing contains a record with missing
  identity is now reported as an incomplete fact (exit 3, UNKNOWN) instead of being filtered (missing
  `head.repo`) or raising an internal error (missing `head`).
- `CheckoutState.branches` and `remote_branches` remain for compatibility and now hold resolved refs
  only; the tri-state methods are authoritative.

## Not done

Stage 6, execution, ledger writing, `--adopt`/`--abandon`, reading Archon's database, running any
probe. Residual limitations are in [technical-debt.md](../technical-debt.md).
