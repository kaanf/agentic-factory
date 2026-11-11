# 0003: Stage 3 approval verification

- Status: proposed (for human review with the Stage 3 PR)
- Date: 2026-11-11
- Inputs: Stage 4B §5 (real human approval), §8 (security model), §10 (Stage 3 acceptance),
  Stage 3 brief, decisions 0001 and 0002.

## Decisions

| # | Decision | Reason |
|---|---|---|
| A1 | Approvals are collected by `factory evidence collect` into the existing `approvals` array; no new command | The brief asks to integrate with the evidence snapshot; one collection, one snapshot |
| A2 | The collector records provenance facts; the planner judges approver membership and staleness | Same split as E1; `planning.py` gains one rule, the engine is otherwise unchanged |
| A3 | Approval is derived from the latest commit that changed the file on the integration head, its single merged PR into the integration branch, and that PR's `merged_by` | 4B §5 mechanism step 2 |
| A4 | The merge commit must be reachable from the head, hold the same content, and its first parent must not already hold it | Rejects content that reached the branch some other way and PRs that did not introduce it |
| A5 | Every collected entry carries `humanAuthorization: "unverifiable"`; the schema has no other value | Agents can use the approver's GitHub credentials (4B §5 constraint). A login proves the credential, not the person |
| A6 | The planner returns UNKNOWN `HUMAN_AUTHORIZATION_UNVERIFIABLE` for such entries and for any approval in a `github` snapshot | An unverifiable approval must never make a task READY, even if the marker is edited out |
| A7 | `credentialIsolation` reports `shared` when the collecting identity is an approver, else `unverified`; there is no `isolated` status | Detecting shared credentials is possible; proving isolation is not, so no outcome can relax A5–A6 |
| A8 | `GET user` is added to the transport allowlist, beside `repos/...` | Needed for A7; still GET-only. The recorder stores only `login` from it |
| A9 | Definite negatives (direct push, unmerged PR, wrong base, already present, absent) are written as `approved: false`; missing, contradictory or ambiguous records omit the entry | Definite facts BLOCK; incomplete ones are UNKNOWN (E3) |
| A10 | Optional manifest field `planningDirectory` locates the project inside the planning repository | Lets the self-describing fixture be verified live against real merge records |
| A11 | `GitHubApi.get`/`get_list` take the endpoint path positionally only | A `path` query parameter (commits by file) collided with the argument name |

## Changes to existing behavior

- Collected snapshots now contain approval entries and a `credentialIsolation` block instead of
  `approvals: []`. Tasks that were UNKNOWN with `APPROVAL_EVIDENCE_MISSING` are now UNKNOWN with
  `HUMAN_AUTHORIZATION_UNVERIFIABLE` (or BLOCKED, or STALE, when GitHub shows a definite problem).
  No task becomes READY.
- `evidence collect` writes one `warning: human authorization unverifiable: ...` line to stderr.
  Exit codes are unchanged.
- Hand-written `simulated` snapshots without the new fields evaluate exactly as before.
- Stage 2 collector tests that inject simulated approvals into a collected snapshot now label it
  `simulated`, because the planner rightly refuses those approvals in a `github` snapshot.
- The recorded GitHub fixture was re-recorded at `dev` = `62c1a3c` (PR #2 merge), so its expected
  facts moved from PR #1 to PR #2.

## Live evidence (2026-11-11, `kaanf/agentic-factory`)

The three fixture artifacts are at the `dev` head with their committed content. Each was last changed
by `5076b60`, which belongs only to PR #2, merged into `dev` by `kaanf` as merge commit `62c1a3c`. The
files are absent at its first parent `4f7b49d`. The collecting identity is `kaanf`, an approver, so
`credentialIsolation.status` is `shared` and every task is UNKNOWN `HUMAN_AUTHORIZATION_UNVERIFIABLE`.
That matches 4B's Stage 3 live acceptance.

## Not done

Credential separation (4B P1–P3, D5, D7, D8) and any check that verifies it; signed-commit or review
based approval; detection of login renames between merge and collection; GitHub Enterprise hosts.
