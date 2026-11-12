# 0008: Stage 5 observation integrity (S1-S5)

- Status: proposed (for a further independent review on PR #5)
- Date: 2026-11-12
- Reviewed head: `e287a61`. Baseline: `34ef0ae` on `dev`.
- Inputs: the third independent review of PR #5 (S1-S3 Medium, S4-S5 Low), decisions 0005-0007,
  Stage 4B §7-§10.

All five findings were reproduced on `e287a61` with one script, run unchanged against both heads.
Because three reviews found adjacent false-absence bypasses, this change addresses the structural
cause as well as the findings. The model is in archon-adapter.md, "Observation integrity".

| # | Finding | Root cause | Fix |
|---|---|---|---|
| S1 | The primary checkout's HEAD named the task branch with no ref; reconcile returned `NO_RUN`, exit 0, while preflight blocked | Occupancy was computed from refs and worktrees only, separately in reconcile and preflight | `CheckoutState.checked_out_state` (primary HEAD and worktrees) is the single occupancy used by both; a HEAD naming a branch that no ref resolves is `HEAD_TARGET_UNRESOLVED` (UNCERTAIN); git metadata is verified as one snapshot, so HEAD changing after it was read is UNKNOWN |
| S2 | `ConfinedTree.list()` closed the directory and every later call re-walked by name; a revision directory replaced between listing and reading turned an existing intent into an empty history | A path-based API: each call resolved names afresh, nothing tied a read to what was listed | Identity ledger in `ConfinedTree`: every observed object's device, inode and type are recorded and every later open must find the same object; enumeration must be stable; files must not change while read; `verify()` re-checks all observations; integrity failures make `read_task_records` unreadable as a whole |
| S3 | macOS `everyone` (gid 12, no listed members) was classified as a private group; an ACL could grant mutation on a `0755` directory | Trust inferred from enumerated group membership and from mode bits alone | Group membership is never trusted: group-writable is refused. ACLs are evaluated (macOS via libc, any allow entry with a modifying right refused; Linux POSIX ACLs bounded by the mask; NFSv4 or rich ACLs and unknown platforms fail closed). The policy applies to every opened directory and file, root path and below. No membership cache |
| S4 | A small, deeply nested YAML document raised `RecursionError` (exit 70) | PyYAML composes recursively | Depth (64) and event (200,000) bounds checked on PyYAML's iterative event stream before composition; `RecursionError` from YAML or JSON becomes a structured document error |
| S5 | Run-record enumeration had no total budget | No accounting | Per-tree budgets enforced during iteration (run records: 10,000 objects, 16 MiB, with rationale); exceeding one makes the records unreadable, never truncated |

Additional structural change: `decide` returns `NO_RUN` only if `_absence_established` holds
(affirmative absence of every relevant fact); otherwise `UNKNOWN` with `ABSENCE_NOT_ESTABLISHED`.

## Behavior changes

- `factory reconcile` exits 3 for `UNCERTAIN` as well as `UNKNOWN` and `COORDINATOR_ACTIVE` (before:
  `UNCERTAIN` exited 0). Other states, blocking or not, exit 0.
- Group-writable directories and files are refused even when the group looks private (decision 0007's
  user-private-group acceptance, added in `e287a61`, is withdrawn). Hosts with umask 002 must use
  umask 022 for Factory Core's paths; the test suite sets umask 022 for its fixtures and tests the
  refusal explicitly. The CI workflow is unchanged.
- Files read below a root must also satisfy the permission policy (owned by root or the current
  user, not group- or world-writable, no modifying ACL).
- Directory enumeration that races with a change, a file changed while read, and any observation
  that no longer holds at the end of a read are refusals.
- Four test functions (eight cases) of the withdrawn private-group acceptance were removed with it
  and replaced by tests of the stricter policy.

## Not done

Stage 6, execution, ledger writing, reading Archon's store, any probe. Residual limitations are in
[technical-debt.md](../technical-debt.md).
