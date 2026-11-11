# Planning model (Factory Core v0.1)

This describes what `factory plan` computes and the evidence snapshot it reads. The snapshot format
is the interface the GitHub collector produces; `plan --live` collects and plans in one step (see
[live-planning.md](live-planning.md)).

## Tri-state results

Every check is `PASS`, `FAIL` or `UNKNOWN`. Combining checks: any `FAIL` gives `FAIL`; otherwise any
`UNKNOWN` gives `UNKNOWN`; otherwise `PASS`. Nothing proceeds unless its result is `PASS`, so
`UNKNOWN` fails closed. A fact missing from the snapshot is `UNKNOWN`, never `PASS` and never
silently `FAIL`.

## Assurance levels (per task, cumulative)

| Level | PASS requires |
|---|---|
| `approved` | Snapshot approval for the task file at its current SHA-256, `approved: true`, `mergedBy` in the manifest's `approvers`, human authorization not marked unverifiable; specification hash unchanged |
| `merged` | Exactly one PR from `<featurePrefix><id>-r<revision>` merged into the repository's integration branch, with a known merge commit |
| `validated` | Integration evidence (below) passes on that PR's merge commit |
| `integration-ready` | The repository's current integration head is itself validated, and every output the task `provides` is present at that head with a valid semantic version |
| `environment-verified` | Always `UNKNOWN` in v0.1 (`ENVIRONMENT_EVIDENCE_UNSUPPORTED`) |

Each level includes every level before it. A merged PR is `merged`, not `validated`: a merged
service PR without push CI on its merge commit never satisfies a `validated` dependency.

### Integration evidence for a commit

Following Stage 4B §3, `validated(sha)` is `PASS` only when all of these hold:

- `reachableFromIntegrationHead` is `true`;
- `mergedPullRequests` contains exactly one PR (and, for a task's merge commit, the task's PR);
- for every check in the manifest's `requiredChecks.integration`, the latest attempt of a run with
  that name, the expected app (default `github-actions`), event `push`, `headBranch` equal to the
  integration branch and the manifest's `workflowPath` concluded `success`.

| Situation | Result |
|---|---|
| Only `pull_request` runs, or no run | `UNKNOWN` (`CHECK_MISSING`) |
| A run with that name from another app or workflow path, and no qualifying run | `FAIL` (`CHECK_PROVENANCE_MISMATCH`) |
| Latest qualifying attempt `failure`, `cancelled`, `timed_out`, `action_required`, `startup_failure` | `FAIL` (`CHECK_FAILED`) |
| Queued or in progress | `UNKNOWN` (`CHECK_IN_PROGRESS`) |
| `neutral`, `skipped`, `stale` | `UNKNOWN` (`CHECK_INCONCLUSIVE`) |
| Two latest-attempt runs that disagree | `UNKNOWN` (`AMBIGUOUS_CHECK_RUN`) |
| Repository declares no required checks | `UNKNOWN` (`NO_REQUIRED_CHECKS`) |

## Gates (per task)

| Gate | Answers | PASS requires |
|---|---|---|
| `start` | May implementation begin? | Task approved, architecture gate passes, not stale, every `gate: start` edge satisfied |
| `openPullRequest` | May the task open a PR? | Task approved, architecture gate passes, not stale. Dependencies are not required, so work against a mocked contract can be reviewed early |
| `merge` | May the PR be merged? | Everything `start` requires plus every `gate: merge` edge. Advisory only in v0.1: nothing enforces it in GitHub yet |
| `complete` | Is the task done? | Own level at least `validated`, not stale, every `gate: complete` edge satisfied |

Safety for downstream consumers is the dependency's level: an edge passes when the dependency has
reached the edge's `requires` level. For edges requiring `integration-ready` or higher, every output
the dependent `consumes` and the dependency `provides` must also satisfy the consumer's version range
(`CONTRACT_INCOMPATIBLE` otherwise).

## Architecture gate

Required for every task kind except `spike`. It passes when:

- the task's `architecture.profileRevision` equals the profile's `revision` (otherwise STALE `PROFILE_REVISION_MISMATCH`);
- the profile file is approved at its current hash (otherwise `PROFILE_NOT_APPROVED`, or STALE `PROFILE_CHANGED_SINCE_APPROVAL`);
- every ADR the profile pins exists, is at the pinned revision (otherwise STALE `ADR_REVISION_MISMATCH`),
  has `status: accepted` (otherwise `ADR_NOT_ACCEPTED`) and is approved at its current hash (otherwise
  `ADR_NOT_APPROVED`, or STALE `ADR_CHANGED_SINCE_APPROVAL`).

`status: accepted` in an ADR is structure, not approval. Approval only ever comes from the snapshot.

## Task state

The first matching row wins.

| State | Condition |
|---|---|
| `STALE` | Any staleness reason: spec, task file, profile or ADR changed since approval, or a revision pin mismatch |
| `COMPLETE` | `complete` gate passes |
| `MERGED` | Level `merged` passes; completion not yet satisfied |
| `BLOCKED` | `start` gate is `FAIL` |
| `UNKNOWN` | `start` gate is `UNKNOWN` |
| `IN_REVIEW` | An open PR from the task branch into the integration branch exists |
| `READY` | `start` gate passes |

## Evidence snapshot (`factory.evidence-snapshot/v1`)

Schema: `src/factory/schemas/evidence-snapshot.v1.schema.json`. Example:
`tests/fixtures/evidence/three-repo-baseline.json`.

```json
{
  "schemaVersion": "factory.evidence-snapshot/v1",
  "project": "example-three",
  "provenance": {"kind": "simulated", "note": "free text"},
  "observedAt": "2026-11-11T12:00:00Z",
  "approvals": [
    {"path": "factory/tasks/SVC-IMPL-007.yaml", "sha256": "<64 hex>", "approved": true,
     "mergedBy": "example-human", "pullRequest": 1}
  ],
  "repositories": {
    "svc": {
      "integrationHead": "<40 hex>",
      "branches": {
        "feat/SVC-IMPL-007-r1": {"pullRequests": [
          {"number": 12, "state": "merged", "baseBranch": "dev", "mergeCommitSha": "<40 hex>"}
        ]}
      },
      "commits": {
        "<40 hex>": {
          "reachableFromIntegrationHead": true,
          "mergedPullRequests": [12],
          "checkRuns": [{"name": "Svc Check", "app": "github-actions", "event": "push",
                         "headBranch": "dev", "workflowPath": ".github/workflows/ci.yml",
                         "status": "completed", "conclusion": "success", "attempt": 1}],
          "outputs": {"svc-contract": {"present": true, "version": "1.2.0"}}
        }
      }
    }
  }
}
```

Rules:

- `provenance.kind` is `simulated` (hand-written or test snapshots) or `github` (written by
  `factory evidence collect`). Plan output always reports `"authenticated": false`: a snapshot is a
  local file the planner cannot verify, and collected approvals cannot be attributed to a human.
- Collected approval entries add `commit`, `mergeCommit`, `evidence` and `humanAuthorization:
  "unverifiable"`; the snapshot adds `credentialIsolation`. An approval with `humanAuthorization`, or
  any approval in a `github` snapshot, is UNKNOWN `HUMAN_AUTHORIZATION_UNVERIFIABLE` at best. See
  [approval-verification.md](approval-verification.md).
- The optional `collection` block (`collector`, `complete`, `errors`) is an audit record written by
  the collector. The planner does not judge it (facts that could not be collected are simply absent),
  but copies it, and `credentialIsolation`, unchanged into the plan's `evidence` when present.
- A snapshot file and a snapshot collected in memory by `plan --live` pass the same validation
  (`evidence.parse_snapshot`).
- A branch listed with `"pullRequests": []` means "no PR exists". An unlisted branch is `UNKNOWN`.
- An omitted commit attribute (`checkRuns`, `mergedPullRequests`, `reachableFromIntegrationHead`,
  `outputs`) is `UNKNOWN` for that attribute.
- Approval paths are project-relative and must be unique. The `sha256` is of the file's raw bytes.
- Duplicate JSON keys and non-finite numbers are rejected.

## Reason codes

Plan reasons nest: a parent such as `DEP_LEVEL_UNMET` carries the dependency's underlying reasons
in `causes`.

| Group | Codes |
|---|---|
| Approval | `TASK_NOT_APPROVED`, `PROFILE_NOT_APPROVED`, `ADR_NOT_APPROVED`, `APPROVAL_EVIDENCE_MISSING`, `NOT_APPROVED`, `APPROVER_NOT_AUTHORIZED`, `HUMAN_AUTHORIZATION_UNVERIFIABLE`, `ADR_NOT_ACCEPTED` |
| Staleness | `SPEC_CHANGED`, `TASK_CHANGED_SINCE_APPROVAL`, `PROFILE_CHANGED_SINCE_APPROVAL`, `ADR_CHANGED_SINCE_APPROVAL`, `PROFILE_REVISION_MISMATCH`, `ADR_REVISION_MISMATCH`, `TASK_STALE` |
| Pull requests | `PR_EVIDENCE_MISSING`, `NOT_MERGED`, `PR_MERGED_INTO_WRONG_BASE`, `AMBIGUOUS_PULL_REQUEST`, `MERGE_COMMIT_UNKNOWN` |
| Commits and checks | `COMMIT_EVIDENCE_MISSING`, `REACHABILITY_UNKNOWN`, `COMMIT_NOT_REACHABLE`, `PR_MAPPING_UNKNOWN`, `COMMIT_NOT_PR_MERGE`, `AMBIGUOUS_PR_MAPPING`, `PR_MAPPING_MISMATCH`, `NO_REQUIRED_CHECKS`, `CHECK_EVIDENCE_MISSING`, `CHECK_MISSING`, `CHECK_PROVENANCE_MISMATCH`, `CHECK_FAILED`, `CHECK_IN_PROGRESS`, `CHECK_INCONCLUSIVE`, `AMBIGUOUS_CHECK_RUN`, `HEAD_NOT_VALIDATED`, `INTEGRATION_HEAD_UNKNOWN`, `TASK_NOT_VALIDATED` |
| Outputs | `OUTPUT_EVIDENCE_MISSING`, `OUTPUT_MISSING`, `OUTPUT_VERSION_UNKNOWN`, `OUTPUT_VERSION_INVALID`, `CONTRACT_INCOMPATIBLE` |
| Dependencies | `DEP_LEVEL_UNMET`, `EVIDENCE_UNKNOWN`, `ENVIRONMENT_EVIDENCE_UNSUPPORTED` |
