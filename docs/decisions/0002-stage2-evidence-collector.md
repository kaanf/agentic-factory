# 0002: Stage 2 GitHub evidence collector

- Status: proposed (for human review with the Stage 2 PR)
- Date: 2026-11-11
- Inputs: Stage 4B §3 (evidence model) and §10 (Stage 2 acceptance), Stage 2 brief, decision 0001.

## Decisions

| # | Decision | Reason |
|---|---|---|
| E1 | The collector only reports facts; all pass/fail rules stay in the Stage 1 planner | One place for evidence rules; planning stays pure and offline; the planner was not changed |
| E2 | Access through `gh api --method GET` behind a small transport interface | Reuses the user's existing GitHub CLI authentication; no token handling; no new runtime dependency; tests replace the transport |
| E3 | A fact that cannot be collected completely is omitted, never partially written | Omission evaluates as `UNKNOWN`; a partial check-run list could hide a failed rerun and yield a false `PASS` |
| E4 | Check-run attempt numbers come from `jobs?filter=all` (the job's `check_run_url` names its check run) | The workflow-runs listing reports only the latest attempt; without per-run attempts, reruns would look like conflicting duplicates |
| E5 | Integration head read before and after collection; if it moved, the head and reachability facts are dropped | Reachability judged against a head that is no longer current cannot be trusted |
| E6 | Rate limiting stops the whole collection | Avoids hammering the API; remaining facts are `UNKNOWN` |
| E7 | Approvals are not collected (`approvals: []`) | Approval derivation is Stage 3, and per 4B §5 it is not verifiable while agents act through the approver's GitHub identity |
| E8 | Snapshot schema v1 gains provenance kind `github` and an optional `collection` audit block | Backward-compatible additions; existing snapshots stay valid; the planner ignores `collection` |
| E9 | `authenticated` stays `false` for every snapshot | A snapshot is an editable local file and approvals are absent; claiming authentication would be false |
| E10 | `--output` inside the project directory is refused | Preserves the Stage 1 guarantee that commands never write to the project |

## Changes to existing behavior

- The snapshot schema accepts provenance `github`. Two Stage 1 tests asserted that `github` was
  invalid; they now assert that an unknown kind (`verified`) is invalid.
- `cli.run` gained an optional `transport` argument (used by tests). Existing commands, exit codes
  and outputs are unchanged.

## Not done (later stages)

Approval verification from merge records (Stage 3), snapshot age limits (`maxAge`), deployment and
environment evidence, a `factory-gate` required check, GitHub Enterprise hosts, and collection
across more than 1,000 items per list.
