# Live planning (Stage 4)

`factory plan <project-dir> --live` validates the project, collects evidence from GitHub, validates
that evidence, and plans, in one read-only operation. It is Stage 4B §10's "`plan` = collector +
approval + engine". Like every Factory Core command it is **advisory**: a plan grants no permission
to do anything, and nothing in Factory Core can act on one.

## Commands

```sh
factory plan <project-dir> --evidence <snapshot.json> [--format text|json]                 # offline
factory plan <project-dir> --live [--save-evidence <snapshot.json>] [--format text|json]    # live
```

Exactly one evidence source is required. Giving neither, or both, is a usage error (exit 64), so
there is no default source and no fallback from one to the other. `--save-evidence` writes the exact
validated snapshot the live plan was computed from; it requires `--live`, must not point inside
the project directory, and is written safely (see "Saving evidence safely"). `factory plan --evidence` behaves exactly as in Stage 3: for every committed
simulated fixture its output is byte-identical to the Stage 3 merge (golden files in
`tests/fixtures/plans/`).

## What `--live` does, in order

| Step | Component (reused) | On failure |
|---|---|---|
| 1. Validate manifest, tasks, profiles, ADRs and cross-references | `validation.validate_project` | Exit 2 before any GitHub request |
| 2. Resolve repositories, integration branches and the planning repository | manifest (validated in step 1) | Exit 2 |
| 3. Open the read-only transport | `github.GhCliTransport` | GitHub CLI missing: exit 3, nothing written |
| 4. Collect repository facts | `collector.collect` (Stage 2) | Fact omitted, collection error |
| 5. Collect approval provenance and credential isolation | `approvals.collect_approvals` via `collect` (Stage 3) | Entry omitted, collection error |
| 6. Validate the in-memory snapshot | `evidence.parse_snapshot`, the same function `--evidence` uses | Exit 2, no plan |
| 7. Confirm integration heads (below) | `collector.read_integration_head` | Anchored facts dropped, collection error |
| 8. Validate again and evaluate | `parse_snapshot`, `planning.evaluate` | |
| 9. Render | `render.to_json` / `render.plan_text` | |

`factory/live.py` holds only this composition. No collector or planner rule changed.

## One validation path

`evidence.parse_snapshot(document, project, source)` is the only way a document becomes a
`Snapshot`. `load_snapshot` reads and parses a file, then calls it; live planning calls it on the
collected document. Both get the same JSON Schema (`evidence-snapshot.v1.schema.json`) and the same
semantic checks (snapshot project matches the manifest, no unknown repositories, no duplicate
approvals). Tests feed the same malformed documents to both paths and require identical issues.

Live planning adds one check on top, about where the evidence came from rather than its shape: the
snapshot must have provenance `github` and a `collection` record (`LIVE_EVIDENCE_NOT_COLLECTED`
otherwise). A live plan can therefore never rest on simulated evidence.

## Plan output

The plan format is `factory.plan/v1` as before. When the snapshot carries them, `evidence` now also
includes the collector's audit records, copied unchanged:

```json
"evidence": {
  "authenticated": false,
  "collection": {"collector": "factory 0.1.0", "complete": false, "errors": [{"repository": "svc", "fact": "repository", "message": "HTTP 502 Bad Gateway"}]},
  "credentialIsolation": {"identity": "kaanf", "status": "shared", "message": "..."},
  "observedAt": "2026-11-11T11:47:38Z",
  "provenance": "github"
}
```

The text output adds `Collection: complete` or `Collection: INCOMPLETE (...)` and `Human
authorization: unverifiable (credential isolation: <status>)`. Hand-written snapshots have neither
record, so their plans are unchanged. A `github` snapshot file planned with `--evidence` now shows
them too. That is an additive change for existing consumers; the planner still does not judge them.

## Saving evidence safely (`--save-evidence`, `evidence collect --output`)

Both options write through `factory/output.py`. Collection takes time, so the destination is checked
before collection and written after it; the design keeps the write from being redirected into the
project in between.

| Property | How |
|---|---|
| Never inside the project | The destination's directory is resolved with every symbolic link removed; the directory, or the destination in it, inside the project is refused (exit 64) before any GitHub request |
| Never through a symbolic link | An existing destination that is a symbolic link (to anywhere) is refused (exit 64). A link created at the destination during collection is replaced, not followed |
| Never into an existing file | The snapshot goes to a new temporary file (`O_CREAT \| O_EXCL \| O_NOFOLLOW`) in the destination's directory and is renamed over the destination name. The old file's contents are never truncated or rewritten, so a hard link to it (including one to a project file) keeps its contents |
| Same directory as checked | The directory is opened before collection, and all later operations are relative to that descriptor. Just before the temporary file is created, and again before the rename, the directory's path must still resolve to itself and name the same directory (device and inode). Otherwise nothing is saved (exit 73) |
| No partial output | The temporary file is written completely and `fsync`ed before the rename, which is atomic; on any failure, including an interrupt, the temporary file is removed and the destination is unchanged |
| Fail closed | A platform without directory-relative file operations is refused (exit 73) |

**Overwrite.** Replacing an existing regular file stays allowed, as in Stage 2 and 3, but now
replaces the directory entry: the new file has a new inode and default permissions (`0666` minus the
umask), other hard links to the old file keep the old contents, and an existing symbolic link,
directory or other non-regular file at the destination is refused. Before this change, the file was
truncated and rewritten in place, and a destination symbolic link outside the project was followed.

**Exit codes.** 64 for a refused destination (inside the project, symbolic link, directory), checked
before any GitHub request. 73 when the snapshot cannot be written (directory missing, unwritable, moved
or replaced, disk full, serialization or rename failure). With exit 73 nothing is printed to stdout and
the destination is left as it was.

**Remaining limitations.**

- In the window between the last directory check and the rename, a process that can write to both the
  output directory's parent and the project can still move the output directory into the project; the
  rename would then create or replace an entry there. POSIX has no portable way to rule this out. It
  requires an attacker who can already modify the project.
- If the directory is moved into the project after the first check, the temporary file can exist there
  briefly before the second check removes it.
- Durability of the rename itself is not forced (the directory is not `fsync`ed); after a crash the
  destination holds either the old or the complete new snapshot.
- Paths on network or FUSE file systems are only as safe as their `rename` and `O_NOFOLLOW` semantics.

## Exit codes

| Code | `plan --evidence` | `plan --live` |
|---|---|---|
| 0 | Plan computed | Plan computed and every fact collected and confirmed |
| 2 | Project or snapshot invalid | Project invalid, or the collected snapshot failed validation (no plan) |
| 3 | n/a | Plan written, but some facts could not be collected or confirmed and evaluate as UNKNOWN; or the GitHub CLI is unusable (nothing written) |
| 64 | Usage error | Usage error, including both or neither source, or a refused `--save-evidence` destination |
| 70 | Internal error | Internal error |
| 73 | n/a | `--save-evidence` could not be written; nothing printed, destination unchanged |

Exit 0 says only that the evidence is complete. It is not authorization: a complete live plan still
has `authenticated: false`, and no task in it can be READY (see below).

## Architecture gate, end to end

The planner already implemented the gate (Stage 1). Stage 4 exercises it against live evidence.

| Situation | Result | Reason codes |
|---|---|---|
| Implementation task's profile has a definite negative finding (`approved: false`: direct push, unmerged PR, wrong base, not on the branch, content already present) | BLOCKED | `PROFILE_NOT_APPROVED` → `NOT_APPROVED` |
| Profile merged by a login not in `approvers` | BLOCKED | `PROFILE_NOT_APPROVED` → `APPROVER_NOT_AUTHORIZED` |
| An ADR the profile pins has `status` other than `accepted` (for example `rejected`) | BLOCKED | `ADR_NOT_ACCEPTED` |
| No approval evidence for the profile or an ADR (omitted, or collection failed) | UNKNOWN | `PROFILE_NOT_APPROVED` / `ADR_NOT_APPROVED` → `APPROVAL_EVIDENCE_MISSING` |
| Approval provenance recorded, human authorization unverifiable (every live approval today) | UNKNOWN | `... → HUMAN_AUTHORIZATION_UNVERIFIABLE` |
| Task pins an older profile revision | STALE | `PROFILE_REVISION_MISMATCH` |
| Profile pins an older ADR revision | STALE | `ADR_REVISION_MISMATCH` |
| Profile or ADR file changed after the content GitHub records | STALE | `PROFILE_CHANGED_SINCE_APPROVAL` / `ADR_CHANGED_SINCE_APPROVAL` |
| Authenticated human approval | Not reachable | Needs a future trusted mechanism (4B P1–P3); nothing in v0.1 can produce it |

State precedence is unchanged: STALE, then COMPLETE, MERGED, BLOCKED (any FAIL), UNKNOWN (any
UNKNOWN, no FAIL), IN_REVIEW, READY. **BLOCKED** means the evidence shows something definitely wrong;
**UNKNOWN** means evidence is missing, inconclusive or unverifiable; **STALE** means the local files no
longer match what was approved or pinned. Each of these stops `start`, `openPullRequest` and `merge`.

**ADR structure** is checked by `validate` (and therefore before any request in `plan --live`), using
the existing `factory.adr/v1` schema: `id`, `title`, `status`, `revision` are required; an ADR with
status `proposed` or `accepted` must list at least one `research` and one `critique` document, and
every listed path must resolve inside the project. A profile that pins an ADR id that does not exist
is `ADR_NOT_FOUND`. All of these are validation errors (exit 2). A revision mismatch between a profile
pin and an ADR is a validation warning and plans as STALE.

**Spikes** (`kind: spike`) need no `architecture` block. If they have one, its references are
validated but the gate does not evaluate it. They are still held to their own task-file approval and specification hash. Every other kind must
reference a profile (schema), or validation fails.

## Source-of-truth rules

| Fact | Source of truth | Not a source |
|---|---|---|
| Task, profile and ADR content and revisions | The local project directory (its SHA-256 digests) | GitHub content, ADR `status` as approval |
| Whether that content was merged, by which login | GitHub merge records on the planning repository's integration head | File contents (self-declared approval fields are rejected) |
| Whether a human authorized the merge | Nothing in v0.1 | GitHub logins, `credentialIsolation`, snapshot provenance |
| PRs, merge commits, check runs, reachability, outputs | GitHub, read at collection time | Agent output, PR bodies, comments |

A local file that differs from the content GitHub recorded is STALE: the plan never assumes the
working tree is what was merged.

## Trust boundary

- `authenticated` is `false` in every plan; the property has no input that can change it.
- Live evidence always has provenance `github`, so every approval in it is at best UNKNOWN
  `HUMAN_AUTHORIZATION_UNVERIFIABLE`, and no task in a live plan can be READY. Removing the
  `humanAuthorization` marker does not help (Stage 3 rule A6).
- The schema has no `verified` human authorization, no `isolated` credential status, and no
  `trusted`, `verified` or `allowDispatch` flag. Unknown fields are rejected.
- READY can come only from a `simulated` snapshot. That is a planning exercise, never permission. A
  future action-capable consumer must refuse `simulated` provenance and every approval without
  verified human authorization (see "Future responsibilities").
- A failed or partial collection never falls back to a snapshot file or to simulated data.

## Determinism and timestamps

`planning.evaluate` is a pure function: identical project files and identical snapshots give
byte-identical JSON and text output, offline or live. In a live plan, `evidence.observedAt` is the
UTC time at which collection **started**, to the second. It is copied from the snapshot, never
generated by the planner, and it is the only clock-dependent field. Two live runs differ otherwise
only if GitHub returned different facts, which is a real change in evidence, not nondeterminism.
Tests pin the clock (`cli.run(..., now=...)`) and the GitHub responses to prove byte-identical live
output, and that a saved live snapshot replans offline to the same plan.

## Collection consistency

GitHub has no atomic multi-request read, and Factory Core does not claim one. The windows are:

1. Per repository (Stage 2): integration head read before and after; if it moved, the head and every
   reachability fact are dropped.
2. Approvals (Stage 3): the planning repository's head read before and after; if it moved, every
   approval is dropped.
3. **New in Stage 4:** after everything is collected, every recorded integration head is read once
   more. A head that moved, or cannot be read, is dropped with every reachability fact anchored to
   it (collection error `integrationHead`, exit 3). Approvals are kept only if the planning
   repository's head is recorded and still current; otherwise they are dropped (collection error
   `approvals`), and approval-dependent results become UNKNOWN `APPROVAL_EVIDENCE_MISSING`.

What remains:

- A head that moves and moves back between reads (for example a force push and its revert) is not
  detected.
- Stage 3 does not record the head it read, so the approvals phase is tied to Stage 2's head only by
  the before/after reads of each window and the final read.
- A plan describes the past. Anything that later acts on it must revalidate the exact revisions it
  acts on at action time (Stage 6).

## Security limitations

- Authentication is whatever the GitHub CLI holds. Factory Core does not isolate credentials, and an
  agent with the same `gh` session can run `plan --live`; that is harmless because the operation is
  read-only and its output authorizes nothing.
- Integrity of a saved snapshot file is not verifiable; that is one reason no snapshot is
  authenticated.
- Remaining collector hardening items are listed in [technical-debt.md](../technical-debt.md).

## Future responsibilities (not implemented)

- **Stage 5** (Archon adapter, dry run, `reconcile`): consumes plans read-only; must treat every
  non-READY state as "do not start" and carry plan provenance into its records.
- **Before Stage 6** (human prerequisites P1–P4): separate agent credentials and a merge restriction,
  and a reviewed mechanism that verifies them, before any approval can be anything but unverifiable.
- **Stage 6** (gated dispatch): refuse `simulated` provenance; refuse approvals without verified
  human authorization; re-collect and revalidate the integration heads, merge commits and file
  hashes it relies on at action time; never act on a plan from an incomplete collection.
