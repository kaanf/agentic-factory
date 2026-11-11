# Technical debt register

Known limitations carried by Factory Core v0.1, as of Stage 5. Classification:

- **Stage 5 blocker**: invalidates a guarantee of the dry run, the run-record reader or read-only
  reconciliation (nothing executes, nothing is written, nothing is invented). Must be fixed in Stage 5.
- **Stage 4 blocker**: invalidates a guarantee of read-only live planning. Must be fixed in Stage 4.
- **Nonblocking hardening**: worth fixing, but every effect fails closed (UNKNOWN or BLOCKED), or
  affects nothing a plan could authorize.
- **Future security prerequisite**: must be resolved before any consumer can act on a plan (Stage 6).

No open item is a Stage 4 or Stage 5 blocker. Stage 5 status of the earlier items: 4 is mitigated for
every value Stage 5 uses (full-match re-validation in `refs.py`; adversarial tests show values that
pass the schema with a trailing newline are refused), but the schemas themselves are unchanged; 6 and
7 are unchanged and remain Stage 6 prerequisites, now guarded by a structural invariant (preflight
`authorization.granted` is never true; decision 0005, S2); 9 does not apply to Stage 5, which writes
nothing.

History: item 6 was narrowed in Stage 4 (end-of-collection head
confirmation). The one blocker found in review, evidence output overwriting a project file through a
hard or symbolic link, was fixed in Stage 4 (decision 0004, L11); its residual race is item 9.

| # | Item | Where | Effect today | Classification |
|---|---|---|---|---|
| 1 | Stage 2 comparison responses are not checked for identity or coherence: `reachableFromIntegrationHead` is read from `status` alone | `collector._RepositoryCollector._reachable` | A wrong or substituted compare response could make `reachable` true, which feeds `validated` and `integration-ready` levels, the `complete` gate and the COMPLETE state. It cannot make anything READY (approvals are unverifiable). Stage 3 already applies the full rules (decision 0003, A17, A19) | Nonblocking for Stage 4; **future security prerequisite** (Stage 6 merge gating depends on dependency levels) |
| 2 | Stage 2 reads any contents 404 as "output absent" | `collector._RepositoryCollector._output` | A 404 for an unconfirmed commit yields FAIL `OUTPUT_MISSING` (BLOCKED) instead of UNKNOWN. Wrong classification, but in the closed direction | Nonblocking hardening |
| 3 | Transport path allowlist uses `re.match` with `$`, which accepts a trailing newline | `github._PATH` | `repos/a/b/pulls\n` passes the allowlist. The method is still hard-coded GET and only `repos/...` and `user` are reachable, so no write is possible | Nonblocking hardening |
| 4 | Object IDs: approvals accept only 40-hex SHA-1; the snapshot schema accepts 40 or 64 hex; JSON Schema `pattern` uses search semantics, so `$` also accepts a trailing newline in `integrationHead` and commit keys; the Stage 2 head is not format-checked | `approvals._SHA`, `evidence-snapshot.v1.schema.json`, `collector.read_integration_head` | A SHA-256 repository would fail closed in Stage 3. A malformed head from GitHub is URL-encoded into later requests, which 404, so its facts are omitted (UNKNOWN). File and live snapshots are validated identically, so neither path bypasses the other | Nonblocking hardening |
| 5 | Compare pagination: GitHub lists at most 250 commits per comparison | `approvals._ApprovalCollector._reachable` (Stage 2 does not look at commits) | Approval entries over longer ranges are omitted (UNKNOWN) | Nonblocking hardening |
| 6 | Cross-phase snapshot atomicity | `collector.collect`, `approvals`, `live._confirm_heads` | Narrowed in Stage 4: every recorded head is re-read after collection, and approvals need a confirmed planning head. A head that moves and moves back between reads is not detected, and the plan is stale the moment it is written | **Future security prerequisite**: action-time revalidation |
| 7 | Simulated evidence authorization boundary | `planning._Evaluator._approval` | `simulated` approvals PASS, so a hand-written snapshot can produce READY. `plan --live` refuses non-`github` evidence, and `authenticated` is always false | **Future security prerequisite**: action-capable consumers must refuse `simulated` provenance |
| 8 | Credential isolation and action-time revalidation | outside Factory Core (4B P1–P3, D4–D8) | Human authorization is unverifiable; every live approval is UNKNOWN | **Future security prerequisite** |
| 9 | Evidence output directory moved into the project between the final check and the rename | `output.SnapshotOutput.save` | Needs a process that can already write to the project and to the output directory's parent, acting in a window of a few system calls. Existing project files are never written through; the rename could create or replace one entry in the moved directory. No portable POSIX primitive closes it | Nonblocking hardening |
| 10 | Archon facts are recorded claims: version from `install.json`, default assistant from `config.yaml`; the binary is never run or hashed | `archon.read_archon_home` | A replaced binary or edited file passes the version check. The dry run authorizes nothing | **Future security prerequisite**: Stage 6 runs `archon --version` (and checks a binary hash) at action time |
| 11 | Preflight and reconcile observations are not atomic: checkout, records, lock and GitHub are read at different moments without holding the lock | `preflight.preflight`, `reconcile.observe` | A state change between reads can give a stale or mixed answer. Inputs (manifest, task) are re-hashed at the end; everything else is advisory | **Future security prerequisite**: Stage 6 recomputes everything under the exclusive lock |
| 12 | Archon's run status is not observed | `reconcile` | An orphaned Archon run after an interrupted dispatch is reported as UNCERTAIN with a pointer to `archon workflow runs`, not detected | Nonblocking hardening; needs a documented Archon read interface |
| 13 | Per-run provider selection in Archon is unverified | `archon.check_assistant` | Tasks whose provider is not Archon's default are refused (`PROVIDER_NOT_ARCHON_DEFAULT`) | Nonblocking (fails closed); revisit after probe 2 |
| 14 | Live bash-only probes not run | `docs/probes/archon-probes.md` | `--base` / `$BASE_BRANCH` / message-argument behavior rests on the earlier manual verification in the pilot workspace, not on a probe of this adapter's exact command | **Future security prerequisite**: run with explicit authorization before Stage 6 |
| 15 | The lock probe takes a shared `flock` for an instant | `runs.probe_lock` | A coordinator trying its exclusive lock at that instant fails and refuses its dispatch | Nonblocking (safe direction) |
| 16 | Linked-worktree and reftable checkouts unsupported; remote-tracking refs are only the last fetch | `gitstate` | Unsupported layouts make local facts UNKNOWN; stale remote-tracking refs can only add refusals (`REMOTE_TRACKING_BRANCH_EXISTS`) | Nonblocking hardening |
| 17 | `archon-implement` opens no pull request | Archon workflow definition | A successful run normally ends as `NO_PR` with an unpushed branch | **Future design item** for Stage 6: decide how the PR is opened (reviewed workflow or human) |
