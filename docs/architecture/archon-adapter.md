# Archon adapter, dry run and reconciliation (Stage 5)

Stage 5 adds everything a dispatch needs except the dispatch itself: a preflight that checks a task
revision against policy and observable state, a deterministic Archon command builder, a reader for
the Stage 6 run records, and read-only reconciliation (Stage 4B §7, §10). **Nothing in this version
starts Archon or a coding agent, and nothing can be made to.** There is no `--execute`, no dispatch
command, and no code path that starts a process other than the existing read-only `gh api --method
GET` transport.

## Commands

```sh
factory preflight <project-dir> --task ID --revision N --checkout DIR (--evidence FILE | --live)
                  [--state-dir DIR] [--archon-home DIR] [--format text|json]
factory reconcile <project-dir> --task ID [--checkout DIR] (--live | --offline)
                  [--state-dir DIR] [--format text|json]
```

- `--checkout` is the task repository's local checkout registered with Archon. Only its git metadata
  and `.archon/config.yaml` are read; no `git` process runs.
- `--state-dir` (default `~/.factory`) holds run records and the dispatch lock. It is only read and is
  never created.
- `--archon-home` (default `~/.archon`) is read for `install.json` (installed version) and
  `config.yaml` (default assistant). Archon itself is not run.
- `preflight --evidence` plans from a snapshot file and reads nothing remote; remote facts are then
  UNKNOWN, so an offline preflight never passes. `preflight --live` plans with `plan --live` and also
  reads the remote `.archon/config.yaml`, the feature branch ref, branch refs of every revision and
  their pull requests, all read-only.
- `reconcile` needs an explicit source: `--live` reads GitHub, `--offline` does not and reports
  remote facts as UNKNOWN. There is no fallback between them.

| Exit | `preflight` | `reconcile` |
|---|---|---|
| 0 | every dry-run check passed. Unreachable in this version: the effective Archon provider is never established (`ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED`), so the best result is UNKNOWN (exit 1). Even a PASS would not be an authorization | reconciled to a well-established state (blocking or not: `NO_RUN`, `PR_OPEN`, `UNTRACKED_LOCAL_BRANCH`, ...) |
| 1 | the dry run did not pass (a check is FAIL or UNKNOWN) | – |
| 2 | invalid project or snapshot | invalid project |
| 3 | another coordinator holds the dispatch lock (refused before any evidence is collected or any request is made), or a live collection was incomplete | the state is `UNKNOWN`, `UNCERTAIN` or `COORDINATOR_ACTIVE`, or any finding is unknown or uncertain |
| 64 | usage error (including any unknown option such as `--execute`) | usage error, or unknown task |
| 70 | internal error | internal error |

Both commands write only to stdout and stderr. JSON output is canonical (sorted keys, no
timestamps), so identical inputs give byte-identical output.

## Manifest and task configuration

Additive and optional fields of `factory.project/v1` and `factory.task/v1`; manifests and tasks
without them stay valid and plan exactly as before.

```yaml
dispatch:
  enabled: false                          # still rejected when true
  archon: {validatedVersion: "0.14.0"}
  workflows:
    default: archon-implement
    allowed: [archon-implement]
    forbidden: []                          # adds to Factory Core's denylist, never replaces it
  providers:
    default: claude
    available:
      claude: {archonAssistant: claude}
      codex: {archonAssistant: codex}
  maxAttemptsPerTaskRevision: 2
```

```yaml
# in a task file (approved content, like every other task field)
execution: {workflow: archon-implement, provider: claude}
```

`factory validate` rejects an allowed workflow that Factory Core denies or the manifest forbids
(`WORKFLOW_FORBIDDEN`), a default workflow or task workflow that is not allowed
(`WORKFLOW_NOT_ALLOWED`), and a default or task provider that is not configured
(`PROVIDER_NOT_AVAILABLE`).

## Workflow and provider policy

Fixed in code (`factory/archon.py`); a manifest can only narrow it.

- **Supported workflows**: `archon-implement`. Its bundled 0.14.0 definition commits on its branch,
  loops until its focused checks pass, and opens no pull request. Any other workflow is
  `WORKFLOW_NOT_SUPPORTED` until its behavior has been reviewed.
- **Denied always**: `archon-merge-queue`, `archon-build-queue`, `archon-intake`,
  `archon-worktree-cleanup`, and any name containing one of the words merge, queue, deploy, release,
  ship, publish, intake, trigger, serve, cleanup, delete. A manifest that allows one fails validation,
  and the preflight and the builder refuse it again independently.
- **Providers**: Archon 0.14.0 has no per-run provider option, and the provider a run uses is
  composed in layers (read from the Archon 0.14.0 binary's configuration code):
  1. the global `~/.archon/config.yaml` `defaultAssistant`;
  2. a repository `.archon/config.yaml` `assistant`, which overrides it;
  3. tier (`small`, `medium`, `large`) and `@alias` bindings from both files, each naming its own
     provider; `archon-implement` resolves `['@implementer', large]`;
  4. per-user AI preferences (whose `defaultProvider` outranks the repository assistant) and
     per-codebase settings, both held in Archon's database;
  5. run layers (`--model`, `--config`), which the builder never emits.

  Preflight checks layers 1-3 in every file Archon may read (the checkout's and, with `--live`, the
  integration branch's repository config): the repository assistant, else the global default, must
  be the task's assistant (`PROVIDER_OVERRIDDEN_BY_REPOSITORY`, `PROVIDER_NOT_ARCHON_DEFAULT`), both
  copies must agree (`ARCHON_REPO_CONFIG_CONFLICT`), and every tier and alias binding in every file
  must name it (`MODEL_BINDING_PROVIDER_MISMATCH`); bindings in forms whose provider is not
  evaluated (string specs, alias indirection) are refused (`MODEL_BINDING_UNSUPPORTED`). A workflow
  file in the checkout's `.archon/workflows` or Archon's home `workflows`/`plugins` that defines the
  selected workflow could replace the reviewed bundled definition (`WORKFLOW_OVERRIDE_PRESENT`).
  Layer 4 cannot be read, so `ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED` (UNKNOWN) is always
  reported: **the effective provider is never established and no preflight passes in Stage 5.**
  Evidence, the acceptance criteria still missing, and a read-only design proposed for future
  approval are in [provider-verification.md](provider-verification.md).

## The command

```text
cwd:  <checkout>
env:  DO_NOT_TRACK=1
argv: archon workflow run <workflow> --branch <featurePrefix><task-id>-r<revision> --base <integration> <message>
```

- An argument vector, never a shell string. `display` quotes it with `shlex` for reading only, and the
  displayed line parses back to exactly the argument vector (tested).
- The builder re-validates every value itself rather than trusting the preflight: task id, feature and
  integration branch (git ref rules, the manifest's branch characters, no component starting with
  `-`, no trailing newline), distinct feature/integration/release branches, workflow policy, absolute
  single-line checkout path, and no control, bidirectional or zero-width characters in any text.
- Only `--branch` and `--base` are emitted. No `--detach`, `--no-worktree`, `--folder`,
  `--container`, `--adopt`, `--supersedes`, `--resume`, `--config`, `--model`, `--input`.
- `--branch` is required because Archon otherwise names the branch itself; `--base` is always
  explicit even when `.archon/config.yaml` agrees. Archon documents that `--branch` **reuses** an
  existing branch, so preflight refuses when the feature branch exists locally, in a worktree, in the
  last fetch or on GitHub.
- The message is one argument with a fixed first line (`Task <id> revision <n>: <title>`), so it can
  never be read as an option, followed by the task file path, specification path and hash, and the
  acceptance criteria, all from the approved task file. Issue bodies, PR comments and other external
  text are never used. At most 16 KiB.
- No credentials appear in the command, its environment or its output.
- The report carries the SHA-256 of the manifest and task files and of the structured command
  (`command.digest`), for a future dispatch to revalidate at action time.

## Preflight checks and reason codes

Each check is PASS, FAIL or UNKNOWN; the dry-run result is FAIL if any check fails, else UNKNOWN if
any is unknown, else PASS. Every applicable reason is reported.

| Area | Codes |
|---|---|
| Task | `TASK_NOT_FOUND`, `TASK_REVISION_MISMATCH` (no command is built), `TASK_TEXT_UNSAFE`, `TASK_READY` (PASS, advisory), `TASK_NOT_READY` (FAIL, or UNKNOWN when the plan state is UNKNOWN), `TASK_READINESS_UNKNOWN` |
| Branches | `INTEGRATION_BRANCH_UNSAFE`, `FEATURE_BRANCH_UNSAFE`, `FEATURE_BRANCH_PROTECTED_NAME`, `BASE_IS_RELEASE_BRANCH` |
| Workflow | `WORKFLOW_NOT_CONFIGURED`, `WORKFLOW_INVALID`, `WORKFLOW_FORBIDDEN`, `WORKFLOW_NOT_ALLOWED`, `WORKFLOW_NOT_SUPPORTED` |
| Provider | `PROVIDER_NOT_CONFIGURED`, `PROVIDER_INVALID`, `PROVIDER_NOT_AVAILABLE`, `PROVIDER_ASSISTANT_INVALID`, `PROVIDER_NOT_ARCHON_DEFAULT`, `PROVIDER_OVERRIDDEN_BY_REPOSITORY`, `ARCHON_REPO_CONFIG_CONFLICT`, `ARCHON_ASSISTANT_INVALID`, `ARCHON_DEFAULT_ASSISTANT_UNKNOWN`, `MODEL_BINDING_PROVIDER_MISMATCH`, `MODEL_BINDING_UNSUPPORTED`, `MODEL_BINDING_INVALID`, `ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED` (always, UNKNOWN) |
| Workflow definitions | `WORKFLOW_OVERRIDE_PRESENT`, `ARCHON_WORKFLOW_DEFINITIONS_UNREADABLE` |
| Archon version | `ARCHON_VERSION_NOT_CONFIGURED`, `ARCHON_VERSION_UNSUPPORTED` (pin outside the adapter's verified set), `ARCHON_VERSION_UNKNOWN`, `ARCHON_HOME_UNREADABLE`, `ARCHON_VERSION_MISMATCH` |
| Checkout | `NOT_A_CHECKOUT`, `CHECKOUT_UNREADABLE`, `CHECKOUT_UNSAFE`, `CHECKOUT_LAYOUT_UNSUPPORTED`, `GIT_METADATA_INVALID`, `GIT_METADATA_UNREADABLE`, `GIT_METADATA_TOO_LARGE`, `CHECKOUT_REMOTE_UNKNOWN`, `CHECKOUT_REPOSITORY_MISMATCH`, `CHECKOUT_ON_FEATURE_BRANCH`, `CHECKOUT_HEAD_UNKNOWN` |
| Archon repository config | `ARCHON_REPO_CONFIG_MISSING`, `ARCHON_REPO_CONFIG_INVALID`, `ARCHON_REPO_CONFIG_UNSAFE`, `ARCHON_REPO_CONFIG_UNREADABLE`, `ARCHON_BASE_BRANCH_UNSET`, `ARCHON_BASE_BRANCH_MISMATCH`, and the same with `ARCHON_REMOTE_` for the integration branch on GitHub; `ARCHON_REMOTE_CONFIG_UNCHECKED` offline. Structured allowlist for both copies: `ARCHON_REPO_HOOK_PRESENT` (`postCreate`, `postCreateTimeout`, `copyFiles`, `initSubmodules`), `ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED` (any key other than `worktree.baseBranch`, `assistant`, `tiers`, `aliases`), `ARCHON_REPO_CONFIG_AMBIGUOUS` (anchors, aliases, merge keys, explicit tags), `ARCHON_REPO_CONFIG_INVALID` (malformed, duplicate keys, not a mapping) |
| Branch state | `FEATURE_BRANCH_EXISTS_LOCAL`, `LOCAL_REF_UNRESOLVED`, `FEATURE_BRANCH_LOCAL_UNKNOWN`, `FEATURE_WORKTREE_EXISTS`, `FEATURE_WORKTREE_UNKNOWN`, `REMOTE_TRACKING_BRANCH_EXISTS`, `REMOTE_TRACKING_UNKNOWN`, `FEATURE_BRANCH_EXISTS_REMOTE`, `REMOTE_BRANCH_UNKNOWN`, `REMOTE_BRANCH_UNCHECKED` |

Local branch facts are tri-state: `present`, `absent` or `unknown`, and only a confirmed absence
passes. A ref entry that exists but cannot be resolved (symbolic ref, symbolic link, malformed or
unreadable value) is present and unresolved; symbolic refs are never followed. A ref directory that
cannot be listed, or a link where a directory could be, makes every name below it unknown. A linked
worktree whose `HEAD` cannot be read or understood makes the worktree question unknown for every
branch. Unresolved entries that concern the task's branches (any revision in the history window, or
a symbolic ref pointing at one) make reconciliation UNCERTAIN; broken refs unrelated to the task are
reported as notes.

GitHub listings are validated the same way. A pull request is skipped only when it is confirmed to
be about something else (a head ref naming another branch, or a head repository naming another
repository); one whose number, head ref, head repository, base, state or merge state is missing or
malformed makes the whole listing incomplete, so the result is UNKNOWN. A matching-refs entry
without a `refs/heads/` name inside the requested prefix and an object id, or a listing of 100 or
more entries (the endpoint gives no completeness signal), is incomplete, never an empty result.
| Reconciliation | `RECONCILE_<STATE>` for every state other than `NO_RUN` (UNKNOWN for `UNKNOWN`, FAIL otherwise) |
| Command | `COMMAND_BUILT` (PASS), or the builder's code: `TASK_ID_UNSAFE`, `FEATURE_BRANCH_UNSAFE`, `INTEGRATION_BRANCH_UNSAFE`, `BRANCH_CONFLICT`, `WORKFLOW_FORBIDDEN`, `CHECKOUT_PATH_UNSAFE`, `UNSAFE_TEXT`, `PROMPT_TOO_LARGE` |
| Inputs | `INPUT_CHANGED_DURING_PREFLIGHT`: manifest or task file hash changed while preflight ran |

### Dry run versus authorization

The report keeps two answers apart:

- `dryRun`: informational consistency checks at one moment. A PASS means "the adapter would build
  this command and saw nothing inconsistent", nothing more.
- `authorization`: always `granted: false`. Its reasons always include `EXECUTION_NOT_IMPLEMENTED`,
  `HUMAN_AUTHORIZATION_UNVERIFIABLE`, `ACTION_TIME_REVALIDATION_MISSING` and
  `EXECUTION_LEDGER_MISSING`, plus `DISPATCH_DISABLED`, `EVIDENCE_NOT_AUTHENTICATED` and
  `EVIDENCE_NOT_COLLECTED` when they apply. `granted` is defined as "no reasons", and the first four
  are unconditional, so no input can grant it: not a READY task, not simulated evidence, not
  collected evidence, not even a manifest with `dispatch.enabled: true` that bypassed validation
  (tested).

This is the trust-laundering invariant: simulated or unauthenticated evidence → READY → dispatch
authority is impossible, because the last arrow does not exist. A simulated snapshot can make a task
READY and a dry run PASS (that is useful for testing the adapter), and the report still says
`granted: false` with the reasons.

## Run records (read only)

```text
<state>/<project>/dispatch.lock
<state>/<project>/runs/<task-id>/r<revision>/attempt-<N>.intent.json    factory.run-intent/v1
<state>/<project>/runs/<task-id>/r<revision>/attempt-<N>.outcome.json   factory.run-outcome/v1
```

Schema assumptions (the Stage 6 writer must produce exactly these):

- **Intent**: project, task, revision, attempt, repository, branch, base, workflow, provider, host,
  `createdAt` (UTC `...Z`), optional `commandDigest`. Created with `O_CREAT|O_EXCL` and fsynced
  before Archon starts.
- **Outcome**: project, task, revision, attempt, `status` (`exited`, `adopted`, `abandoned`),
  `exitCode` (required integer when `exited`), `finishedAt`, optional `archonRunId`. Written to a
  temporary file, fsynced and renamed.
- Attempts are numbered from 1 without gaps; a record's fields must match its location.

The reader (`factory/runs.py`) validates path components before use and reads through a confined,
no-follow directory walk: symbolic links anywhere below the state directory are refused, records
must be regular single-link files of at most 64 KiB, JSON is parsed strictly. It reports, never
repairs: `RECORD_CORRUPT`, `RECORD_SCHEMA_INVALID`, `RECORD_CONTRADICTS_PATH`, `RECORD_SYMLINK`,
`RECORD_HARD_LINKED`, `RECORD_TOO_LARGE`, `RECORD_UNREADABLE`, `OUTCOME_WITHOUT_INTENT`,
`ATTEMPT_GAP`, `LATER_ATTEMPT_AFTER_INTERRUPTION`, `TEMPORARY_FILE_PRESENT`, `UNEXPECTED_ENTRY`,
`RECORD_PATH_UNSAFE`, `STATE_UNREADABLE`, `RECORDS_UNREADABLE`. Every one of them makes
reconciliation UNCERTAIN.

The lock is probed with a shared, non-blocking `flock` that is released at once; the file is opened
read-only and never created. A probe at the same instant as a coordinator's exclusive attempt can
make that coordinator refuse, which is the safe direction.

`factory preflight` probes the lock before anything else, including `--live` evidence collection,
and refuses with exit 3 and no GitHub request when it is held; `preflight()` probes it again itself.
This is a Stage 5 observation, **not** Stage 6 exclusive dispatch locking: the lock can be taken or
released immediately after the probe, so the early check avoids wasted reads and requests but proves
nothing about any later moment.

**The reader is not the ledger.** Exclusive locking, atomic intent creation and atomic outcome
publication are Stage 6 obligations. Nothing in Stage 5 writes a record.

## Reconciliation decision table

`reconcile.decide` is a pure function of the observations. The first matching row wins; every
finding is reported regardless.

| # | Condition | State | Blocks dispatch | Human action |
|---|---|---|---|---|
| 1 | dispatch lock held by another process | `COORDINATOR_ACTIVE` (nothing else is read, exit 3) | yes | none; retry reconcile later |
| 2 | any contradiction: interrupted dispatch (intent, no outcome) in any revision, corrupt or contradictory records, records or branches for a newer revision, PR into the wrong base, several active PRs, checkout of another repository | `UNCERTAIN` | yes | inspect; for an interrupted run check `archon workflow runs`, then adopt or abandon (manual until Stage 6) |
| 3 | any needed fact unreadable: GitHub failure, offline, no checkout, unreadable state (including a state root refused by the filesystem trust contract), the checkout's HEAD neither a branch nor an object id (`CHECKOUT_HEAD_UNKNOWN`), lock unknown | `UNKNOWN` | yes | make the facts readable |
| 4 | an older revision's PR is open | `SUPERSEDED_PR_OPEN` | yes | close it or revert the revision bump; it never counts for this revision |
| 5 | no record; remote branch or any PR exists | `UNTRACKED_BRANCH` | yes | adopt (manual) or remove the branch yourself |
| 6 | no record; local branch, or the branch checked out by the primary HEAD or a worktree | `UNTRACKED_LOCAL_BRANCH` (a HEAD naming the branch with no resolvable ref is `HEAD_TARGET_UNRESOLVED`, row 2) | yes | inspect; finish or remove (`archon complete`) |
| 7 | records; current PR merged | `PR_MERGED` | yes | none |
| 8 | records; current PR open | `PR_OPEN` | yes | review |
| 9 | records; last outcome abandoned | `ABANDONED` (plus `BRANCH_PRESENT_AFTER_ABANDON` while the branch exists) | yes | remove the branch before another attempt |
| 10 | records; PR closed unmerged | `PR_CLOSED` | yes | revise the task or remove the branch |
| 11 | records; exited non-zero, no PR | `RUN_FAILED` | yes | revise or decide on another attempt; never retried |
| 12 | records; exited 0 or adopted, no PR | `NO_PR` (plus `BRANCH_NOT_PUSHED` when not on GitHub) | yes | open a PR yourself or abandon; never retried |
| 13 | rows 9-12 with attempts used ≥ `maxAttemptsPerTaskRevision` | `ATTEMPTS_EXHAUSTED` | yes | revise the task (new revision) |
| 14 | every absence fact affirmatively established (see Observation integrity) | `NO_RUN`; otherwise `UNKNOWN` with `ABSENCE_NOT_ESTABLISHED` | no | none |

Older revisions are examined for PRs, branches and records (up to 50 back; earlier ones make the
result UNKNOWN via `HISTORY_TRUNCATED`). A merged older revision appears only under `history` as
`OLDER_REVISION_MERGED`, explicitly "not evidence or approval" for the current revision; it never
changes the state. `MULTI_HOST_UNSUPPORTED` is always reported: only this host's records are
visible (4B D10). Archon's own run status is never read; an interrupted dispatch says so
(`ARCHON_RUN_STATUS_NOT_OBSERVED`).

How each §7 row is reproduced: interrupted dispatch → row 2; competing coordinator → row 1;
multi-host → informational finding plus row 5 for a foreign host's pushed branch; local branch with
intent → row 2, without → row 6; remote branch without record → row 5; success without PR → row 12;
superseded open PR → row 4; older merged revision → history only. Each has a test in
`tests/unit/test_reconcile.py`.

`--adopt` and `--abandon` are not implemented. They would be the first write path into the run
records, and a correct one needs the Stage 6 lock and atomic publication; until then the human
actions above are manual.

## Security boundaries

| Boundary | Mechanism | Test |
|---|---|---|
| No process execution | only `github.py` imports `subprocess`; no module calls `os.system`, `os.exec*`, `os.spawn*`, `os.fork`, `os.posix_spawn` | static AST scan; behavioral test with every process primitive patched to fail |
| No writes in Stage 5 modules | no write primitives, write flags or `open()` in `archon`, `fsread`, `gitstate`, `preflight`, `reconcile`, `refs`, `runs` | static AST scan; fingerprint of project, checkout, state and Archon home before and after |
| GitHub read-only | existing GET-only transport; reconcile requests only `repos/<owner>/<repo>/...` | request log assertions |
| No execution surface | CLI has no execute, dispatch, run, adopt, abandon, retry, merge, push, delete, force or yes options or commands | parser walk; `--execute` exits 64 |
| Untrusted strings are data | full-match validators (`refs.py`), argv not shell, display round-trip, control and bidi characters refused | adversarial tests: `$(...)`, backticks, `;`, option-like text, trailing newlines that pass JSON Schema |
| Filesystem confinement | `fsread.ConfinedTree` under the trust contract below: one descriptor-relative walk from `/`, no resolve-then-open and no restart; ancestors modifiable only by root or the current user; only root-controlled relative symbolic links followed; concurrent change around any lookup refused; per-component `O_NOFOLLOW` below the root, regular files, bounded size, optional single link, `O_NONBLOCK` against FIFOs | directory replacement injected before and after each acquisition step (ancestor, parent, root, entry identity), absolute/relative/chained/cyclic root links, shared writable ancestors, symlink, hard-link, FIFO, traversal and size tests; production `read_task_records` chain |
| Bounded work | history window computed before allocation; only existing remote revisions are stored | revisions 1 to 1,000,000: bounded entries, requests and memory |
| TOCTOU between validation and command | command built from the in-memory project the checks used; manifest and task re-hashed at the end; hashes and command digest in the report | file changed mid-preflight → `INPUT_CHANGED_DURING_PREFLIGHT` |
| Evidence trust | authorization structurally never granted | simulated READY, every provenance, `dispatch.enabled` bypass |

## Observation integrity

Three security reviews of this stage found false absences: an observation that was dropped, unread,
read from the wrong object or read at a different moment silently produced `NO_RUN` or PASS. The
structural cause was that `NO_RUN` was the fall-through when no finding was raised, so every
observation failure that did not raise its own finding increased confidence. Each earlier fix
patched one producer of findings. This section is the model that now holds instead.

**Invariants.**

1. `NO_RUN` requires complete, coherent, affirmative evidence of absence.
2. Every relevant unknown or contradictory observation blocks reconciliation (UNKNOWN / UNCERTAIN,
   exit 3).
3. A selected filesystem object cannot silently be replaced by another between listing and reading.
4. Filesystem permissions are not evidence of object identity, and unsafe or unevaluable permissions
   never establish trust.
5. Stage 5 never authorizes or performs execution.

**Transitions.** Each local or remote fact is tri-state: present (`True`), absent (`False`, only
from a complete observation), unknown (`None`). Findings carry a result: `INFO` (never affects the
state), `BLOCKING`, `UNKNOWN`, `UNCERTAIN`. The state is `COORDINATOR_ACTIVE` if the lock is held;
otherwise `UNCERTAIN` if any finding is uncertain, `UNKNOWN` if any is unknown, then the
well-established states of the decision table. `NO_RUN` is additionally guarded by
`_absence_established`: lock absent or free, current-revision records readable and empty, every
examined revision's records free of problems, every examined revision's pull requests read, no
records for newer revisions, and the current branch's local ref, checked-out state, remote branch
and pull-request list each explicitly absent. If any fails, the state is `UNKNOWN`
(`ABSENCE_NOT_ESTABLISHED`) even if no finding said so. `blocksDispatch` is false only for `NO_RUN`.

**Where false absence came from, and what holds now.**

| Pattern | Sites found | Now |
|---|---|---|
| A missing key or collection entry read as absence | local occupancy ignored the primary HEAD (S1) | one occupancy for refs, primary HEAD and worktrees, used by reconcile and preflight; a HEAD naming a branch with no resolvable ref is UNCERTAIN |
| A malformed file ignored | refs and worktree metadata (F1) | kept as present-but-unresolved or unknown |
| A failed read becoming an empty collection | records of a replaced directory (S2) | integrity failures make the whole observation unreadable |
| A partial listing taken as complete | directory enumeration | a listing is complete only if the directory did not change while enumerated |
| A path reopened after inspection | every `ConfinedTree` call re-walked from the root by name (S2; also git refs, worktrees, workflow files) | identity ledger: every later open must find the recorded device and inode |
| Observations from different moments combined | `read_checkout`, `read_task_records`, Archon readers | `verify()` re-checks every observation at the end of the logical read |
| An informational note suppressing a blocking condition | HEAD state (R1) | task-relevant conditions are UNKNOWN or UNCERTAIN, never INFO |

## Filesystem identity and trust contract

Everything Stage 5 reads outside the project (run records, checkout git metadata and Archon
configuration, Archon's home) goes through `fsread.ConfinedTree`:

| Term | Meaning |
|---|---|
| Configured path | The string from the command line or default. Untrusted; relative paths are anchored at the working directory |
| Symbolic links in the configured path | Followed only if the link and its directory are owned by root, the directory is not group- or world-writable, and the target is relative (at most 40). Anything else: `ROOT_PATH_SYMLINK_UNTRUSTED` or `ROOT_PATH_SYMLINK_UNSUPPORTED` |
| Root acquisition | One walk from `/`, each component opened relative to the previous descriptor; around each lookup the parent's modification and status-change times and the entry's device and inode must not change (`ROOT_CHANGED_DURING_ACQUISITION`). Never retried, never redirected |
| Identity ledger | Every object observed through one tree (traversed directory, listed entry, file read, entry found absent) is recorded with device, inode and type. Any later open of that path must find the same object (`IDENTITY_CHANGED`); below the root every step is descriptor-relative with `O_NOFOLLOW` and the opened object must be the one `lstat` found |
| Complete listing | Enumeration of a directory is accepted only if its modification and status-change times did not change during it (`DIRECTORY_CHANGED`) |
| Stable file | A file must not change while it is read (`FILE_CHANGED`) |
| Coherent snapshot | `verify()` at the end of `read_task_records`, `read_checkout` and each Archon reader re-checks every observation: listed directories unchanged, files unchanged, absences still absent (`TREE_CHANGED`) |
| Budgets | Per tree: objects observed and bytes read (`BUDGET_EXCEEDED`); run records 10,000 objects and 16 MiB (rationale in `runs.py`), other trees 250,000 objects and 64 MiB |

Integrity codes (`INTEGRITY_CODES`) never mean "absent": run records become unreadable (reconcile
UNKNOWN or UNCERTAIN), checkout facts unknown, Archon facts unknown.

### Permission and ACL trust model

Applied to every directory the root walk looks inside and to every directory and file opened below
the root:

- owned by root or the current user;
- not group- or world-writable, except a directory with the sticky bit;
- no ACL grants another principal a modifying right.

Group membership is never used to establish trust. Whether a group contains only the current user
cannot be established authoritatively with the system interfaces available: macOS has implicit
groups (`everyone`, gid 12, has no listed members but contains every user), nested and
directory-service groups exist on both platforms. A group-writable object is therefore refused,
whatever its group (S3).

ACLs: on macOS every extended ACL entry is read through libc (`acl_get_fd_np`; the ABI was verified
against real `chmod +a` ACLs). Any *allow* entry with a modifying right (write or add file, append or
add subdirectory, delete, delete child, write attributes, write extended attributes, write security,
change owner) is refused, whoever it names, including the owner; *deny* entries only restrict and are
accepted (macOS home directories carry `group:everyone deny delete`). On Linux, POSIX access ACL
entries for other principals are limited by the ACL mask, which is the group-class mode bits, so the
mode check covers them; NFSv4 and rich ACLs are not evaluated and are refused. Any platform where
ACLs cannot be evaluated, and any ACL that cannot be read, fails closed (`PERMISSIONS_UNVERIFIABLE`).
Linux ACL behavior is covered by a CI test only when `setfacl` is available there; it is not claimed
verified by the macOS tests.

**Operational consequence.** Hosts with umask 002 (Ubuntu user private groups, GitHub runners)
create group-writable checkouts and state, which Factory Core refuses. Use umask 022 (or remove group
write) for checkouts, the state directory and Archon's home. The test suite builds its fixtures with
umask 022 and tests the refusal of group-writable objects explicitly.

Out of scope: a process running as the current user that changes a path before it is first observed
(the operator's own principal; 4B P2 moves agents to another OS user).

## Limitations

- **Observations are a snapshot.** Nothing here holds a lock while reading, and reading several files
  or API responses is not atomic. Every result is advisory; Stage 6 must recheck under its lock.
- **The effective provider is not established.** Archon's database-held layers (per-user AI
  preferences, per-codebase settings) can change it and are not readable; every preflight is
  therefore at best UNKNOWN.
- **Parsing is bounded.** YAML is limited to 64 levels and 200,000 events, checked on the iterative
  event stream before any node is composed (`YAML_TOO_COMPLEX`); deeply nested JSON is refused
  instead of exhausting the stack.
- **Configured paths must be real paths.** A symbolic link the current user owns anywhere in
  `--checkout`, `--state-dir` or `--archon-home` is refused; only root-controlled relative system
  links (macOS `/tmp`, `/var`, `/etc`; merged `/usr` on Linux) are followed.
- **The root path itself is configuration.** `ConfinedTree` pins the tree once its walk reaches it;
  a directory replaced *before* the walk reaches it simply changes what the configured path names.
  Use paths whose ancestors only you can write.
- **Archon facts are recorded claims.** The installed version comes from `install.json`, not from
  running the binary, and the default assistant from `config.yaml`. A replaced binary or edited file
  is not detected. Stage 6 must check `archon --version` (and preferably a binary hash) at action time.
- **Archon run status is not observed.** Its SQLite store has no documented read interface.
- **Remote-tracking refs** in the checkout are only the last fetch; they are reported, never treated
  as remote evidence.
- **Linked-worktree and reftable checkouts** are unsupported (UNKNOWN).
- **Single host only.** Work started from another host is visible only once it reaches GitHub.
- **Archon behavior assumptions not re-observed** (the executable probe was removed; see
  [archon-probes.md](../probes/archon-probes.md)):
  `--base` sets both the worktree start point and `$BASE_BRANCH`; arguments after the workflow name are
  joined into the message; no option inside the single message argument is parsed.

## Stage 6 prerequisites

Human prerequisites, outside Factory Core (4B §10):

- **P1** Machine user and classic PAT without the `workflow` scope, write collaborator on every
  repository.
- **P2** Separate macOS user for agent runs holding only machine-user and provider credentials.
- **P3** "Restrict updates" ruleset on the integration and release branches with admin bypass,
  confirmed first in a throwaway repository.
- **P4** Push CI on every integration branch; strict mode on all protected branches; required checks
  pinned to GitHub Actions.

Factory Core obligations for Stage 6, derived from Stage 5: exclusive `flock` on `dispatch.lock`
held across plan, preflight and Archon start; intent with `O_CREAT|O_EXCL` and fsync; outcome via
temporary file, fsync and rename; action-time revalidation of heads, approvals, input hashes,
command digest and the Archon binary; establishing the effective Archon provider including its
database-held layers (a documented read interface, or per-user preferences provably disabled for the
agent OS user); refusal when the agent environment's GitHub identity is an
approver; `dispatch.enabled` set only by a human-merged PR; the live probe acceptance item met (by an approved manual procedure) for the pinned
Archon version in every repository.
