# Approval verification (Stage 3)

`factory evidence collect` also reads the merge record behind every task, architecture profile and
ADR file, and writes it to the snapshot's `approvals`. It records **GitHub provenance**. It does
**not** establish **human authorization**, and while agents can use the approver's GitHub
credentials it cannot. No collected approval ever passes in `factory plan`.

## Trust boundary

| Question | Can v0.1 answer it? |
|---|---|
| Is this exact content (SHA-256) on the planning repository's integration head? | Yes: read from GitHub, blob SHA checked |
| Which commit last changed it, and which merged PR put that commit on the integration branch? | Yes, or the approval is omitted |
| Which GitHub login merged that PR? | Yes (`merged_by`) |
| Did a human, rather than an agent using that login's credentials, make the merge? | **No.** Recorded as `humanAuthorization: "unverifiable"` |

A GitHub username shows which credential performed a merge, not which person intended it. Today,
agents can run `gh` with the owner's token, so a merge by `kaanf` may have been made by an agent.
Factory Core does not attempt to tell these apart, and it has no credential-separation mechanism.
The snapshot schema has no "verified" value for human authorization, so nothing in v0.1 can claim it.

The snapshot also carries a `credentialIsolation` record built from `GET /user` (the identity the
GitHub CLI is using for collection):

| `status` | Meaning |
|---|---|
| `shared` | The collecting identity is in the manifest's `approvers`: agent and human credentials are demonstrably shared |
| `unverified` | The collecting identity is unknown or not an approver. This is **not** evidence of isolation: an agent may hold other credentials, including the approver's |

Both outcomes leave every approval unverifiable. `factory evidence collect` prints the reason as a
warning on stderr.

## What is checked, per file

Paths are project-relative; `planningDirectory` in the manifest locates the project inside the
planning repository (default: the repository root).

1. `GET contents/{path}?ref={integration head}`: 404 → `approved: false`, `NOT_ON_INTEGRATION_BRANCH`.
   Otherwise the content's git blob SHA must equal the reported `sha`, and its SHA-256 is recorded.
2. `GET commits?path={path}&sha={head}&per_page=1`: the latest commit that changed the file.
3. `GET commits/{commit}/pulls`: no PR → `NO_PULL_REQUEST` (direct push); no PR into the integration
   branch → `PR_NOT_INTO_INTEGRATION_BRANCH`; none merged → `PR_NOT_MERGED`; more than one merged
   into the integration branch → ambiguous, omitted.
4. `GET pulls/{n}`: must be merged into the integration branch, with a merge commit and a `merged_by`
   login that agree with the listing; otherwise omitted.
5. `GET compare/{merge}...{head}`: the merge commit must be reachable from the head; otherwise omitted.
6. `GET contents/{path}?ref={merge}`: must equal the head content; otherwise omitted.
7. `GET commits/{merge}` and the file at its first parent: if the content was already there, the PR
   did not introduce it → `CONTENT_PRESENT_BEFORE_MERGE`.

All checks pass → `approved: true`, `evidence: MERGED_PULL_REQUEST`, with `commit`, `mergeCommit`,
`pullRequest` and `mergedBy`. This works for merge commits (the latest change is a feature-branch
commit) and squash merges (it is the squash commit). For rebase merges where the file changed before
the last rebased commit, step 6 or 7 fails closed.

The integration head is read before and after; if it moved, every approval is dropped.

## How the planner judges an entry

In order, for the task, profile or ADR file:

| Entry | Result |
|---|---|
| None (omitted, or collection failed) | UNKNOWN `APPROVAL_EVIDENCE_MISSING` |
| `sha256` differs from the local file | STALE `*_CHANGED_SINCE_APPROVAL` (modified after the merge, locally or on the head) |
| `approved: false` | FAIL `NOT_APPROVED` (the evidence code is in the message) |
| `mergedBy` not in `approvers` | FAIL `APPROVER_NOT_AUTHORIZED` |
| `humanAuthorization` present, or snapshot provenance `github` | UNKNOWN `HUMAN_AUTHORIZATION_UNVERIFIABLE` |
| otherwise (hand-written `simulated` snapshots only) | PASS |

So a collected approval at best yields UNKNOWN, which blocks `start`, `openPullRequest` and `merge`
and keeps every implementation task out of READY. Removing the marker from a collected snapshot does
not help, because `github` provenance alone triggers the rule.

## Failure rules

As for the rest of the collector: missing, contradictory or ambiguous records omit the file's entry
and add a `collection.errors` item (exit code 3); a rate limit stops all further requests. Only the
affected file is omitted.

## Prerequisites for trustworthy approvals (not done; 4B P1–P3, D4–D8)

- A machine user for agents, with no access to the approver's credentials (P1).
- Agent runs under a separate macOS user holding only machine-user and model-provider credentials (P2).
- A "Restrict updates" ruleset on `dev`/`main` so only the human can merge (P3).
- A verification step that checks the above. Until one exists and has been reviewed,
  `humanAuthorization` stays `unverifiable`, and adding a "verified" value is a schema change that
  needs explicit human approval.
