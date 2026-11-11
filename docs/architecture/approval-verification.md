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

1. `GET contents/{path}?ref={integration head}`: 404 at a confirmed head commit (see "When a 404
   proves absence") → `approved: false`, `NOT_ON_INTEGRATION_BRANCH`. Otherwise the content's git blob SHA must equal the reported `sha`, and its SHA-256 is recorded.
2. `GET commits?path={path}&sha={head}&per_page=1`: the latest commit that changed the file.
3. `GET commits/{commit}/pulls`: every record must have a positive integer `number` and a base
   branch that is a valid git branch name (see "Branch names"), and its `base.repo.full_name` must be the planning repository; otherwise omitted. No PR →
   `NO_PULL_REQUEST` (direct push); no PR into the integration branch →
   `PR_NOT_INTO_INTEGRATION_BRANCH`; none merged → `PR_NOT_MERGED`; more than one merged into the
   integration branch → ambiguous, omitted. A PR counts as not merged only if its record has an
   explicit `merged_at: null` and a `state` of `open` or `closed`. If `merged_at` is absent or not an
   ISO 8601 timestamp, or the record contradicts itself (`open` with a merge time, or a `merged` flag
   that disagrees with `merged_at`), the entry is omitted. That applies to every PR into the
   integration branch, so one unmerged PR does not settle the question while another is unknown.
4. `GET pulls/{n}`: must be the record of PR `n` (same number), in the planning repository, based on
   the integration branch, `merged: true`, with a valid merge status (as in step 3), a 40-hex merge
   commit and a `merged_by` login. Its merge time and merge commit must equal the listing's.
   Otherwise omitted.
5. `GET compare/{merge}...{head}`: the response must describe exactly these two commits and show the
   merge commit is the head or an ancestor of it (see "Compare responses"); otherwise omitted.
6. `GET contents/{path}?ref={merge}`: must equal the head content; otherwise omitted.
7. `GET commits/{merge}` and the file at its first parent: if the content was already there, the PR
   did not introduce it → `CONTENT_PRESENT_BEFORE_MERGE`. If it is not there, that counts only once
   the parent commit is confirmed to exist. The record must describe the merge commit
   (`sha` equal to it), and `parents` must be a non-empty list whose first item has a 40-hex `sha`.
   Otherwise the introduction check cannot run, so the entry is omitted. It is never skipped.

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
and add a `collection.errors` item (exit code 3, `collection.complete: false`); a rate limit stops
all further requests. Only the affected file is omitted.

Every commit SHA taken from a GitHub response (integration head, latest commit, merge commit, first
parent) must be exactly 40 lowercase hex characters, with nothing before or after, before it is used
in a request. A malformed ref would make the contents API answer 404, which this collector reads as
"file absent"; validating first keeps a bad ref from turning into a finding.

### When a 404 proves absence

The contents API answers 404 both for a path missing from an existing commit and for a commit that
does not exist (or that the token cannot see), and its body does not reliably tell them apart. So a
contents 404 counts as "file absent at commit C" only when `GET git/commits/{C}` returns a record
whose `sha` is C and whose `tree.sha` is a well-formed SHA. That is checked lazily, on the first 404
for C, and cached for the rest of the collection; commit SHAs are immutable, so the cache cannot go
stale, and the integration head is still re-read at the end. The same rule covers every contents
lookup: integration head, merge commit and first parent.

| `git/commits/{C}` | Result |
|---|---|
| Record for C with a valid tree | The 404 is absence: `NOT_ON_INTEGRATION_BRANCH` at the head, "differs" (omitted) at the merge, "introduced by this PR" at the first parent |
| 404 | Commit does not exist or is not accessible → omitted |
| Other HTTP error, transport failure | Omitted with that error (a rate limit stops collection) |
| Record of another commit, missing or malformed tree, not an object | Omitted |

The repository itself was resolved at the start of collection (renames and transfers are rejected).

### Compare responses

`compare/{merge}...{head}` is accepted only as a coherent statement about exactly those commits:

- `base_commit.sha` must equal the requested merge commit.
- `ahead_by`, `behind_by` are non-negative integers, `total_commits` equals `ahead_by`,
  `merge_base_commit.sha` is a SHA, and `commits` is a list of records with SHAs.
- The status must agree with the counts and the merge base, following git's semantics:

  | Status | Must hold | Reachable? |
  |---|---|---|
  | `identical` | merge = head, both counts 0, merge base = merge | yes |
  | `ahead` | merge ≠ head, `ahead_by` > 0, `behind_by` = 0, merge base = merge | yes |
  | `behind` | merge ≠ head, `ahead_by` = 0, `behind_by` > 0, merge base = head | no (omitted) |
  | `diverged` | both counts > 0, merge base is neither | no (omitted) |

- When `ahead_by` > 0, `commits` must list all of them and include the head. The response has no
  head field, so this is how its head is identified. Without pagination, GitHub lists at most 250
  commits; a longer range therefore cannot be confirmed and the entry is omitted.

Anything else, including an unknown status, is omitted. Reachability is never read from the status
string alone.

### Branch names

PR base branches, and the configured integration branch, must be names `git check-ref-format
--branch` accepts. No control characters, space, `~ ^ : ? * [ \`, `..` or `@{`; no empty, leading
or trailing `/`; no component starting with `.` or ending in `.lock`; no trailing `.`; no leading
`-`; not `HEAD` or `@`. A malformed base branch omits the entry (UNKNOWN). A valid branch other than
the integration branch is still the definite `PR_NOT_INTO_INTEGRATION_BRANCH` (BLOCKED). The manifest
schema admits some names git rejects (`dev.`, `-dev`, `dev/.x`, `HEAD`); with such an integration
branch, approval collection records one error and collects nothing.

### UNKNOWN versus BLOCKED

Absence of evidence is not evidence of absence. `approved: false` (BLOCKED) is written only when the
records conclusively show a negative fact. A record with a missing or malformed field that the
judgment depends on is never read as that field's "false" value.

## Remaining limitations

- The first parent is confirmed to exist, but not to have been on the integration branch before the
  merge. It comes from the merge commit's record, and the merge commit is confirmed reachable from
  the head.
- `git/commits/{C}` can also serve commits that live only in a fork of the repository (GitHub shares
  objects across a fork network). Every commit this collector looks up comes from a record of the
  planning repository itself, which limits but does not remove that ambiguity.
- Comparisons over more than 250 commits are omitted rather than paginated.
- Only 40-hex (SHA-1) object IDs are accepted.
- The Stage 2 collector's own reachability and output-file checks are not held to these rules yet.

## Deferred: simulated evidence and action-capable consumers

`simulated` snapshots are unauthenticated by construction: anyone can write one, and the planner
lets their approvals PASS so the planning engine can be exercised offline. That is safe only while
`factory plan` is advisory and dispatch is disabled. Any future consumer that acts on a plan
(dispatch, merging, opening pull requests) must refuse `simulated` provenance, and every approval
not backed by verified human authorization, before acting. Stage 3 does not implement that
restriction, because nothing in it can act.

## Prerequisites for trustworthy approvals (not done; 4B P1–P3, D4–D8)

- A machine user for agents, with no access to the approver's credentials (P1).
- Agent runs under a separate macOS user holding only machine-user and model-provider credentials (P2).
- A "Restrict updates" ruleset on `dev`/`main` so only the human can merge (P3).
- A verification step that checks the above. Until one exists and has been reviewed,
  `humanAuthorization` stays `unverifiable`, and adding a "verified" value is a schema change that
  needs explicit human approval.
