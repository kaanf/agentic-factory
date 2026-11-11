# GitHub evidence collector (Stage 2)

`factory evidence collect <project-dir> [--output FILE]` reads GitHub through the authenticated
GitHub CLI and writes an evidence snapshot (`factory.evidence-snapshot/v1`, provenance `github`)
that `factory plan --evidence` evaluates unchanged. `factory plan --live` runs the same collector in memory and plans
its result directly (see [live-planning.md](live-planning.md)).

## Separation of concerns

The collector **reports facts**; the Stage 1 planner **judges** them. The collector applies no
pass/fail rules about events, branches, apps, workflow paths, attempts or PR mapping. It records
them faithfully, and `planning.py` applies the rules in [planning-model.md](planning-model.md).
Planning stays pure and offline.

| Module | Role |
|---|---|
| `factory/github.py` | Read-only transport (`gh api --method GET`), status and rate-limit detection, pagination with completeness checks |
| `factory/collector.py` | Turns API responses into snapshot facts |
| `factory/cli.py` | `evidence collect` command |

## What is collected, per manifest repository

| Snapshot fact | Source |
|---|---|
| repository identity | `GET repos/{repo}`; `full_name` must equal the manifest's `github` (a renamed or transferred repository is not trusted) |
| `integrationHead` | `GET repos/{repo}/git/ref/heads/{integration}`, read before and after collection; if it moved, the head and all reachability facts are dropped |
| `branches[<prefix><id>-r<rev>]` | `GET repos/{repo}/pulls?state=all&head={owner}:{branch}`; PRs whose head is a fork are ignored; `mergeCommitSha` is recorded only for merged PRs (an open PR's value is a test merge) |
| commits | The integration head and every merge commit of a task PR merged into the integration branch |
| `reachableFromIntegrationHead` | `GET repos/{repo}/compare/{sha}...{head}`: `ahead` or `identical` → true; `behind` or `diverged` → false |
| `mergedPullRequests` | `GET repos/{repo}/commits/{sha}/pulls`, keeping only merged PRs whose `merge_commit_sha` equals the commit |
| `checkRuns` | `GET repos/{repo}/commits/{sha}/check-runs?filter=all` (every attempt). For `github-actions` runs, the event, head branch and workflow path come from the workflow run with the same `check_suite_id` (`GET .../actions/runs?head_sha={sha}`), and the attempt from the job whose `check_run_url` names the check run (`GET .../actions/runs/{id}/jobs?filter=all`). Runs from other apps get empty provenance, so they can never satisfy a required check. Runs whose `head_sha` differs are dropped. |
| `approvals`, `credentialIsolation` | Merge records of the planning repository; see [approval-verification.md](approval-verification.md) |
| `outputs` (head only) | `GET repos/{repo}/contents/{path}?ref={head}`; 404 → absent; the version is read with the manifest's JSON Pointer and must be a string |

PR bodies, comments and any agent output are never read.

## Failure rules

- **All or nothing per fact.** If any request for a fact fails, or a list cannot be shown to be
  complete (`total_count` mismatch, more than 10 pages, a check run without a workflow run or attempt
  number), the whole fact is omitted. The planner then evaluates it as `UNKNOWN`. A partial
  check-run list is never written, because it could hide a failed rerun.
- **Rate limits** (HTTP 429, or 403 with `x-ratelimit-remaining: 0` or a rate-limit message) stop
  all further requests for that collection. Every fact not yet collected is absent.
- **Exit codes:** 0 when everything was collected; 3 when the snapshot was written but is
  incomplete (each missing fact is listed on stderr and in `collection.errors`), or when the GitHub
  CLI is unusable; 2 for an invalid project; 64 for usage errors, including an `--output` path inside
  the project directory or one that is a symbolic link; 73 when `--output` cannot be written.
- `--output` is written atomically through a temporary file and never through an existing file or
  link; see "Saving evidence safely" in [live-planning.md](live-planning.md).
- The collector validates its own output against the snapshot schema before writing it.

## Safety

- The transport can only issue `GET` requests, only to `repos/{owner}/{name}/...` paths and `user`
  (the collecting identity) on the GitHub CLI's default host, and it rejects `.` and `..` path segments. There is no write code path.
- Authentication is whatever `gh` already holds; the collector never reads or stores tokens.
- The project directory is never written to.

## Not authenticated

A `github` snapshot records what the API returned at `observedAt`. It is still not treated as
authenticated:

- the snapshot is a local file that can be edited after collection;
- approvals record GitHub merge provenance, but human authorization is unverifiable while agent and
  human GitHub identities are not separate (4B §5, D4–D8), so no task can reach `READY` from a
  collected snapshot. See [approval-verification.md](approval-verification.md).

## Recorded fixture

`tests/fixtures/github/agentic-factory-recorded.json` holds real responses for
`tests/fixtures/agentic-factory-project`, a project describing this repository. Re-record with
`python tests/github_support.py` (read-only GET requests).
