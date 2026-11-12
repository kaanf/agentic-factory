# 0009: Observation consistency contract (PROPOSED, not accepted)

- **Status: PROPOSED. Requires a human architecture decision. Nothing in this ADR is implemented as
  a contract change**; Stage 5 code keeps its current outputs and fields until it is accepted.
- Date: 2026-11-12
- Trigger: fourth independent review of PR #5, findings Q1 (non-atomic final verification) and Q2
  (root namespace and trust over time), after three earlier remediation cycles.
- Reviewed head: `c91aaf9`.

## Why the remediation cycles kept failing

Every cycle added a verification at a later moment: re-read heads (Stage 4), descriptor-relative
walks (F3), trust checks on the path (R2), an identity ledger and a final `verify()` (S2). Each one
detects a class of change that happened before it ran. None of them makes a concurrent writer wait,
so none can establish that the state did not change *after* it ran, and none can make observations
of different sources (lock, run records, checkout, GitHub) refer to one moment. Object identity
(device, inode) answers "is this the object I saw?", not "did the state stay the same over time?".
Another verification pass cannot turn a sequence of reads into an atomic snapshot.

## Q1 analysis

Schedule (reproduced on `c91aaf9`): reconcile lists `r2` and `r3` (both empty); `verify()` re-checks
`r2` (unchanged); a writer creates `r2/attempt-1.intent.json`; `verify()` re-checks `r3`
(unchanged) and passes. Result: `NO_RUN`, `blocksDispatch=false`, exit 0, while the intent exists.

- **Was there a valid consistency point?** Per source, yes, under an assumption. Each observation
  i was made at time rᵢ and re-checked at tᵢ; if every mutation of an observed object changes its
  device, inode, size, or advances its modification or status-change time, observation i held on
  [rᵢ, tᵢ]. All rᵢ precede the start of `verify()`, and all tᵢ follow it, so every observation held
  at the moment `verify()` started. The run records were correctly empty at that moment.
- **Can the algorithm prove it?** Not in general. The assumption fails when a mutation does not
  advance the recorded times beyond their observed values: filesystems with coarse timestamps
  (Linux ext4/XFS use a clock tick of milliseconds), clock adjustments, or in-place rewrites within
  one tick that keep size and inode. Detection is therefore best-effort, not a proof.
- **What does the code claim?** The Stage 5 documents call the result a "coherent snapshot" whose
  observations "describe one moment" (true per source, best-effort), and define `NO_RUN` as
  "affirmative evidence of absence" with `blocksDispatch=false` (true only of a past moment). Read
  as "no run exists when someone acts on this", the claim is **false**, and it could not be true for
  any read-only observer: a writer can always act after the last read.
- **Across sources** there is no common moment at all: the lock is probed, then records are read,
  then the checkout, then GitHub.
- **What could enforce a stronger guarantee:** only cooperation from every writer (B) or a storage
  layer with transactions or snapshots (C). See below.

## Options

| | A. Diagnostic observation with explicit temporal limits | B. Cooperative locking and generation tracking | C. Strong snapshots or transactional storage |
|---|---|---|---|
| Security property | Each result is a best-effort description of a past moment per source; change detected during the read refuses (fail closed) | Against cooperating writers: reads under a shared lock see no concurrent Factory Core write; a generation counter detects writes between reads | A read sees exactly one committed state |
| Provable | Only "each refusal is justified" and "no execution follows"; never absence at a later time | Consistency against writers that take the lock, nothing more | Consistency of the snapshotted store only |
| Who must cooperate | Nobody | Every writer of the protected data (Stage 6 dispatcher, `--adopt`/`--abandon`). Archon (git worktrees and branches), GitHub and other local processes do not and cannot | Every writer must use the store; git and GitHub are outside it |
| Crashes | Read-only; no effect | `flock` released by the kernel; atomic rename keeps records whole (4B §7) | Store-dependent recovery |
| Malicious local mutation | Detected best-effort; not prevented | A writer ignoring the lock is not stopped; only OS-user separation (4B P2) removes the same-user writer | Same: root or same-user writers can bypass or destroy the store |
| Cross-platform | Yes | `flock` on macOS and Linux (4B §7 already relies on it) | Filesystem snapshots need privileges and specific filesystems; SQLite is portable |
| Stage 5 cost | Documentation and output wording | Reader side only (shared lock while reading records); creating the lock file would be a write, so it applies only when the file exists. No writer exists in Stage 5 | High; contradicts 4B C7 ("No database") |
| Stage 6 dependency | Stage 6 must re-establish everything under its own lock | Stage 6 writer holds `LOCK_EX` across recomputed plan, preflight and `O_EXCL` intent creation (4B §7) | Stage 6 would write the store |
| 4B compatibility | Matches 4B: reconcile is read-only and advisory; "the plan is recomputed inside the lock" | Matches 4B §7 exactly | Conflicts with C7 |

## Recommendation

**A for Stage 5, B in Stage 6. Reject C.** Stage 5 results are diagnostic: they describe what was
observed during a stated window and refuse when they detect change. The execution-time guarantee
4B needs is "no second dispatch for this revision", and it comes from Stage 6: the exclusive lock
held while the plan and preflight are recomputed and the intent is created with `O_CREAT|O_EXCL`
before Archon starts. Mutation by non-cooperating actors (Archon's own git changes, GitHub, other
processes of the same OS user) is outside that guarantee; 4B already accepts that the remote-branch
check "narrows but does not close the race" and relies on OS-user separation (P2).

## Proposed evidence contract (requires approval)

| Term | Proposed meaning |
|---|---|
| PRESENT | An observation found the object, and it was still the same object when the observation ended |
| ABSENT | A complete observation of the containing directory or listing found no such object; a statement about that observation only |
| UNKNOWN | The fact could not be observed completely, trustworthily or coherently (unreadable, untrusted, changed during observation, budget, offline) |
| UNCERTAIN | Observations were made but contradict each other or show an unresolved operation |
| NO_RUN | No evidence of a run for the revision was found by observations made during the reported window, and every relevant fact was affirmatively observed as absent. **It is not a guarantee that no run exists or can start later** |
| blocksDispatch | Whether the observed state, as reported, would block a dispatch decision. `false` never authorizes and never substitutes for Stage 6's recomputation under its lock. Proposed rename: `blocksDispatchAtObservation` (schema `factory.reconcile/v2`) |
| Observed-at | The window in which the observations were made. Proposed: `observation.window` with start and end supplied through the existing injected clock, so output stays byte-identical in tests (4B T13); omitted when no clock is injected |
| Evidence completeness | Every fact the decision needs was observed (no UNKNOWN) |
| Snapshot consistency | Per source, best-effort: the observations of one source are re-checked before it is used and the source is refused if a change is detected. Not atomic; not across sources |
| Execution authorization | Never granted by Stage 5 (`authorization.granted` structurally false). In Stage 6 it requires the lock, recomputation, P1-P4 and approval verification, never a Stage 5 report |

## Conflicts

- **With 4B:** none found. 4B §7 makes reconcile read-only and advisory and puts the guarantee in
  the locked dispatch path.
- **With Stage 5's own documents:** Invariant 1 and the "coherent snapshot" wording imply more than
  can be provided. They are not rewritten until this ADR is decided; the architecture document
  carries an explicit note pointing here.
- **With determinism (4B T13):** an observed-at window must come from an injected clock to keep
  output reproducible.

## Migration if accepted

1. Rewrite the Observation-integrity invariants and the reconcile table with the meanings above.
2. Rename `blocksDispatch` (schema `factory.reconcile/v2`), add `observation.window`; keep `v1`
   output only if a consumer needs it (none exists).
3. Stage 6 design: shared lock for reconcile readers when the lock file exists; exclusive lock,
   recomputation and `O_EXCL` intent for the dispatcher; generation counter optional.
4. Tests: replace the strict-xfail Q1 test with tests of the window and of the Stage 6 lock protocol.

## Decision required

1. Accept A (Stage 5) + B (Stage 6), reject C?
2. Accept the evidence contract, including the restated `NO_RUN`?
3. Rename `blocksDispatch` and bump the reconcile schema, or keep the name with the restated meaning?
4. Add `observation.window` (injected clock)?
