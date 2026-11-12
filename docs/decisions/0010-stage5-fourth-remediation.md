# 0010: Stage 5 fourth review remediation (Q2-Q5; Q1 deferred to decision 0009)

- Status: proposed (for review with PR #5)
- Date: 2026-11-12
- Reviewed head: `c91aaf9`. Baseline: `34ef0ae` on `dev`.
- Related: [0009](0009-observation-consistency-contract.md) (PROPOSED observation consistency
  contract; requires a human decision).

All five findings were reproduced on `c91aaf9` with one script, run unchanged before and after.
Only changes that keep or tighten the current contract are implemented here.

| # | Finding | Status | Change |
|---|---|---|---|
| Q1 | An intent inserted into `r2` after `verify()` re-checked `r2` and before it finished: `NO_RUN`, `blocksDispatch=false`, exit 0 | **Not fixed; blocked at the decision gate** | No mechanism available to a read-only Stage 5 observer can see changes after it observed; enforcing more needs cooperating writers (Stage 6) or a changed contract. Analysis, options and recommendation in 0009. The schedule is kept as a strict-xfail test (`raises=AssertionError`). Documentation now states the temporal limits explicitly; outputs and fields are unchanged |
| Q2 | Root permissions or ACL loosened after acquisition, and the configured root replaced or renamed during observation, were not detected | Fixed (best-effort detection) | The root's policy is re-checked on every walk; `verify()` re-applies the policy to every file read and re-walks the configured path, which must still name the acquired root (`ROOT_CHANGED`, an integrity code). Trust failures keep their own code (`UNSAFE_PERMISSIONS`). Missing roots: absence observed in a directory that passes the trust policy only |
| Q3 | Remote-tracking refs blocked preflight but were ignored by reconcile (present or unknown) | Fixed | Reconcile carries a tri-state `tracking` fact (output `remoteTrackingRef`): unknown is UNKNOWN, present while GitHub shows no branch is UNCERTAIN (`REMOTE_TRACKING_CONTRADICTS_REMOTE`), and `NO_RUN` requires it to be absent. Both consumers now block on the same conditions |
| Q4 | Recursive aliases raised `RecursionError` (exit 70); alias amplification exhausted CPU | Fixed | Conservative YAML subset enforced on the event stream before composition: 1 MiB input budget, no anchors, aliases, merge keys or explicit tags, scalar unique keys, depth and event bounds; oversized integers are structured errors |
| Q5 | A JSON integer beyond Python's digit limit raised `ValueError` (exit 70) | Fixed | `parse_json` maps it to `INVALID_JSON` (the digit limit is kept) and has a 64 MiB input budget; a record with one is corrupt (UNCERTAIN), never an empty history |

Also: run-record problems that mean the records could not be observed (integrity codes,
`STATE_UNREADABLE`) now make reconcile UNKNOWN instead of UNCERTAIN; both block and exit 3.

## CI evidence, stated precisely

The PR checks are `pull_request` runs: they test GitHub's synthetic merge ref (`refs/pull/5/merge`)
and report against the head SHA. For `c91aaf9` the synthetic merge commit `e76b9ba` had the same tree
as the head (the base is an ancestor of the head). Per 4B F1 that is not integration evidence:
push CI on the exact integration SHA exists only after a merge into `dev`, and none has happened.
