"""Stage 4: `factory plan --live` composes validation, collection, approval provenance and planning.

Every scenario uses mocked or recorded GitHub responses; sockets are disabled for every test. The
synthetic project is a Python service with one ADR, one profile, an implementation task and a spike.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import codes, task_of
from github_support import (
    BASE,
    RECORDED,
    SELF_PROJECT,
    FakeTransport,
    Sequence,
    commit_entry,
    comparison,
    contents,
    git_commit,
    identity,
    listing,
    load_recorded,
    not_found,
    page,
    pull,
    pull_detail,
    q,
    ref,
)
from support import EVIDENCE, FIXTURES, SINGLE_REPO, THREE_REPO, copy_project, three_repo_baseline, write_snapshot

from factory import cli as cli_module
from factory import github, live
from factory.collector import CollectionResult
from factory.evidence import load_snapshot, parse_snapshot
from factory.github import GitHubError, Response
from factory.project import load_project


def sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()  # noqa: S324 - fake commit ids


HEAD = sha("integration head")
MOVED = sha("integration head after a later merge")
MERGE = sha("squash merge of the planning PR")
PARENT = sha("integration head before the planning PR")
DIRECT = sha("direct push")
CLOCK = lambda: datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # noqa: E731

TASK = "factory/tasks/SVC-1.yaml"
SPIKE = "factory/tasks/SPIKE-1.yaml"
PROFILE = "factory/profiles/svc.yaml"
ADR = "docs/adr/ADR-1-storage.md"
ARTIFACTS = (ADR, PROFILE, SPIKE, TASK)
ADR_TEXT = """---
schemaVersion: factory.adr/v1
id: ADR-1
title: Store ledger entries in SQLite
status: accepted
revision: 1
research: [docs/adr/research/ADR-1-options.md]
critique: [docs/adr/research/ADR-1-critique.md]
---
# ADR-1: Store ledger entries in SQLite
"""
APPROVALS_UNANCHORED = (
    "the planning repository's integration head is not confirmed for the whole collection, "
    "so approval provenance cannot be tied to it"
)
CONVENTIONS = [pytest.param("dev", "feat/", id="dev-feat"), pytest.param("trunk", "topic/", id="trunk-topic")]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_project(root: Path, integration: str = "dev", prefix: str = "feat/") -> Path:
    files = {
        "factory/project.yaml": f"""schemaVersion: factory.project/v1
project: {{id: ledger-service}}
approvers: [example-human]
planningRepository: svc
repositories:
  - id: svc
    github: example-org/example-svc
    branches: {{integration: {integration}, release: stable, featurePrefix: {prefix}}}
    requiredChecks:
      integration:
        - {{name: "pytest", workflowPath: .github/workflows/test.yml}}
architecture:
  adrDirectory: docs/adr
  profiles:
    - {{id: svc-arch, repository: svc, path: {PROFILE}}}
dispatch: {{enabled: false}}
""",
        PROFILE: """schemaVersion: factory.profile/v1
id: svc-arch
revision: 1
repository: svc
adrs:
  - {id: ADR-1, revision: 1}
enforcedBy:
  - check: "pytest"
""",
        ADR: ADR_TEXT,
        "docs/adr/research/ADR-1-options.md": "# Options: SQLite, PostgreSQL, flat files\n",
        "docs/adr/research/ADR-1-critique.md": "# Independent critique of ADR-1\n",
        "specs/svc-1.md": "# Ledger entry endpoint\n",
        "specs/spike-1.md": "# Spike: measure SQLite write throughput\n",
        TASK: f"""schemaVersion: factory.task/v1
id: SVC-1
revision: 1
title: Ledger entry endpoint
repository: svc
kind: implementation
specification: {{path: specs/svc-1.md, sha256: {_digest("# Ledger entry endpoint\n")}}}
architecture: {{profile: svc-arch, profileRevision: 1}}
acceptance: [POST /entries stores an entry]
""",
        SPIKE: f"""schemaVersion: factory.task/v1
id: SPIKE-1
revision: 1
title: Measure SQLite write throughput
repository: svc
kind: spike
specification: {{path: specs/spike-1.md, sha256: {_digest("# Spike: measure SQLite write throughput\n")}}}
acceptance: [Throughput is recorded]
""",
    }
    project = root / "ledger-service"
    for path, text in files.items():
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        (project / path).write_text(text)
    return project


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path)


def world(project: Path, integration: str = "dev", prefix: str = "feat/", user: Any = None) -> dict[str, Any]:
    """GitHub as it would be if one squash-merged PR introduced every artifact, merged by the approver."""
    responses: dict[str, Any] = {
        BASE: identity(),
        f"{BASE}/git/ref/heads/{integration}": ref(HEAD),
        page(f"{BASE}/pulls", head=f"example-org:{prefix}SVC-1-r1", state="all"): [],
        page(f"{BASE}/pulls", head=f"example-org:{prefix}SPIKE-1-r1", state="all"): [],
        page(f"{BASE}/commits/{HEAD}/pulls"): [],
        page(f"{BASE}/commits/{HEAD}/check-runs", filter="all"): listing("check_runs", []),
        q(f"{BASE}/compare/{HEAD}...{HEAD}"): comparison(HEAD, HEAD, "identical"),
        "user": Response(401, {"message": "Requires authentication"}) if user is None else user,
        page(f"{BASE}/commits/{MERGE}/pulls"): [pull(7, f"{prefix}planning", base=integration, merged_sha=MERGE)],
        f"{BASE}/pulls/7": pull_detail(7, f"{prefix}planning", merged_sha=MERGE, base=integration),
        q(f"{BASE}/compare/{MERGE}...{HEAD}"): comparison(MERGE, HEAD, "ahead"),
        f"{BASE}/commits/{MERGE}": commit_entry(MERGE, [PARENT]),
        f"{BASE}/git/commits/{PARENT}": git_commit(PARENT),
    }
    for path in ARTIFACTS:
        text = (project / path).read_text()
        responses[q(f"{BASE}/contents/{path}", ref=HEAD)] = contents(text)
        responses[q(f"{BASE}/commits", path=path, per_page=1, sha=HEAD)] = [commit_entry(MERGE)]
        responses[q(f"{BASE}/contents/{path}", ref=MERGE)] = contents(text)
        responses[q(f"{BASE}/contents/{path}", ref=PARENT)] = not_found()
    return responses


def invoke(*args: str | Path, transport: Any = None, clock: Any = CLOCK) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli_module.run([str(a) for a in args], out=out, err=err, transport=transport, now=clock)
    return code, out.getvalue(), err.getvalue()


def live_plan(
    project: Path, responses: dict[str, Any], *args: str | Path, expect: int = 0, clock: Any = CLOCK
) -> tuple[dict[str, Any], str, FakeTransport]:
    transport = FakeTransport(responses)
    code, out, err = invoke("plan", project, "--live", "--format", "json", *args, transport=transport, clock=clock)
    assert code == expect, err or out
    return json.loads(out), err, transport


def simulated(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The same facts relabelled as a hand-written snapshot, with approvals that a planner may accept."""
    document = {k: v for k, v in snapshot.items() if k not in ("collection", "credentialIsolation")}
    document["provenance"] = {"kind": "simulated"}
    document["approvals"] = [
        {k: v for k, v in a.items() if k not in ("humanAuthorization", "evidence")} for a in snapshot["approvals"]
    ]
    return document


def saved_snapshot(project: Path, responses: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    target = tmp_path / "saved.json"
    live_plan(project, responses, "--save-evidence", target)
    result: dict[str, Any] = json.loads(target.read_text())
    return result


def offline_plan(project: Path, snapshot: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    path = write_snapshot(snapshot, tmp_path / "offline.json")
    code, out, err = invoke("plan", project, "--evidence", path, "--format", "json")
    assert code == 0, err or out
    result: dict[str, Any] = json.loads(out)
    return result


def all_states(plan: dict[str, Any]) -> set[str]:
    return {t["state"] for t in plan["tasks"]}


# -- B, V: explicit live planning ---------------------------------------------------------------


@pytest.mark.parametrize(("integration", "prefix"), CONVENTIONS)
def test_live_plan_composes_collection_approvals_and_planning(tmp_path: Path, integration: str, prefix: str) -> None:
    project = make_project(tmp_path, integration, prefix)
    plan, err, transport = live_plan(project, world(project, integration, prefix))
    assert plan["evidence"] == {
        "authenticated": False,
        "collection": {"collector": "factory 0.1.0", "complete": True, "errors": []},
        "credentialIsolation": {
            "identity": None,
            "message": "there is no evidence that agent and human GitHub credentials are separated, so a merge by an "
            "approver's login cannot be attributed to a human",
            "status": "unverified",
        },
        "observedAt": "2026-11-11T12:00:00Z",
        "provenance": "github",
    }
    assert err.startswith("warning: human authorization unverifiable: there is no evidence")
    impl = task_of(plan, "SVC-1")
    assert impl["state"] == "UNKNOWN"
    assert impl["branch"] == f"{prefix}SVC-1-r1"
    assert impl["architecture"]["result"] == "UNKNOWN"
    assert {"PROFILE_NOT_APPROVED", "ADR_NOT_APPROVED", "HUMAN_AUTHORIZATION_UNVERIFIABLE"} == codes(
        impl["architecture"]["reasons"]
    )
    assert "READY" not in all_states(plan)
    # Every request was a read the transport allowlist admits; the final head check is one of them.
    assert all(re.match(r"^(?:user|repos/example-org/example-svc(/|$))", path) for path in transport.requests)
    assert transport.requests.count(f"{BASE}/git/ref/heads/{integration}") == 5


def test_live_plan_of_recorded_real_responses_equals_collect_then_plan(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "collected.json"
    code, _, _ = invoke("evidence", "collect", SELF_PROJECT, "-o", snapshot_path, transport=load_recorded())
    assert code == 0
    outputs = {}
    for fmt in ("json", "text"):
        offline = invoke("plan", SELF_PROJECT, "--evidence", snapshot_path, "--format", fmt)
        online = invoke("plan", SELF_PROJECT, "--live", "--format", fmt, transport=load_recorded())
        assert online[0] == offline[0] == 0
        assert online[1] == offline[1]
        outputs[fmt] = online[1]
    plan = json.loads(outputs["json"])
    for task_id in ("evidence-collector", "approval-verification"):
        assert task_of(plan, task_id)["state"] == "UNKNOWN"
        assert "HUMAN_AUTHORIZATION_UNVERIFIABLE" in codes(task_of(plan, task_id)["gates"]["start"]["reasons"])


def test_saved_live_snapshot_replans_offline_to_the_same_plan(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "live.json"
    online, _, _ = live_plan(project, world(project), "--save-evidence", target)
    assert offline_plan(project, json.loads(target.read_text()), tmp_path) == online


# -- C, D, E: architecture profile approval state ------------------------------------------------


def test_explicitly_rejected_profile_blocks_the_implementation_task(project: Path, tmp_path: Path) -> None:
    responses = world(project)
    # The profile reached the integration branch by a direct push: a definite negative finding.
    responses[q(f"{BASE}/commits", path=PROFILE, per_page=1, sha=HEAD)] = [commit_entry(DIRECT)]
    responses[page(f"{BASE}/commits/{DIRECT}/pulls")] = []
    plan, _, _ = live_plan(project, responses)
    impl = task_of(plan, "SVC-1")
    assert impl["state"] == "BLOCKED"
    assert impl["architecture"]["result"] == "FAIL"
    profile = next(r for r in impl["architecture"]["reasons"] if r["code"] == "PROFILE_NOT_APPROVED")
    assert profile["result"] == "FAIL"
    assert [c["code"] for c in profile["causes"]] == ["NOT_APPROVED"]
    assert "NO_PULL_REQUEST" in profile["causes"][0]["message"]
    # The spike needs no profile and is not blocked by it.
    assert task_of(plan, "SPIKE-1")["state"] == "UNKNOWN"


def test_explicitly_rejected_profile_blocks_offline_too(project: Path, tmp_path: Path) -> None:
    snapshot = simulated(saved_snapshot(project, world(project), tmp_path))
    assert task_of(offline_plan(project, snapshot, tmp_path), "SVC-1")["state"] == "READY"  # control
    next(a for a in snapshot["approvals"] if a["path"] == PROFILE)["approved"] = False
    impl = task_of(offline_plan(project, snapshot, tmp_path), "SVC-1")
    assert impl["state"] == "BLOCKED"
    assert codes(impl["gates"]["start"]["reasons"]) == {"PROFILE_NOT_APPROVED", "NOT_APPROVED"}


def test_rejected_adr_blocks_the_implementation_task(project: Path) -> None:
    (project / ADR).write_text(ADR_TEXT.replace("status: accepted", "status: rejected"))
    impl = task_of(live_plan(project, world(project))[0], "SVC-1")
    assert impl["state"] == "BLOCKED"
    assert "ADR_NOT_ACCEPTED" in codes(impl["architecture"]["reasons"])


def test_missing_profile_approval_evidence_is_unknown(project: Path) -> None:
    responses = world(project)
    responses[q(f"{BASE}/commits", path=PROFILE, per_page=1, sha=HEAD)] = Response(500, {"message": "Server Error"})
    plan, err, _ = live_plan(project, responses, expect=3)
    impl = task_of(plan, "SVC-1")
    assert impl["state"] == "UNKNOWN"
    profile = next(r for r in impl["architecture"]["reasons"] if r["code"] == "PROFILE_NOT_APPROVED")
    assert (profile["result"], [c["code"] for c in profile["causes"]]) == ("UNKNOWN", ["APPROVAL_EVIDENCE_MISSING"])
    assert f"incomplete: svc: approvals.{PROFILE}: HTTP 500 Server Error" in err
    assert plan["evidence"]["collection"]["complete"] is False


def test_missing_profile_approval_alone_keeps_an_otherwise_ready_task_unknown(project: Path, tmp_path: Path) -> None:
    snapshot = simulated(saved_snapshot(project, world(project), tmp_path))
    snapshot["approvals"] = [a for a in snapshot["approvals"] if a["path"] != PROFILE]
    impl = task_of(offline_plan(project, snapshot, tmp_path), "SVC-1")
    assert impl["state"] == "UNKNOWN"
    assert codes(impl["gates"]["start"]["reasons"]) == {"PROFILE_NOT_APPROVED", "APPROVAL_EVIDENCE_MISSING"}


@pytest.mark.parametrize("user", [None, {"login": "example-human"}], ids=["identity-unknown", "shared-identity"])
def test_unverifiable_human_authorization_is_unknown(project: Path, user: Any) -> None:
    plan, _, _ = live_plan(project, world(project, user=user))
    impl = task_of(plan, "SVC-1")
    assert impl["state"] == "UNKNOWN"
    for gate in ("start", "openPullRequest", "merge"):
        assert impl["gates"][gate]["result"] == "UNKNOWN"
    start = {r["code"]: codes(r["causes"]) for r in impl["gates"]["start"]["reasons"]}
    assert start == {
        "ADR_NOT_APPROVED": {"HUMAN_AUTHORIZATION_UNVERIFIABLE"},
        "PROFILE_NOT_APPROVED": {"HUMAN_AUTHORIZATION_UNVERIFIABLE"},
        "TASK_NOT_APPROVED": {"HUMAN_AUTHORIZATION_UNVERIFIABLE"},
    }
    assert plan["evidence"]["credentialIsolation"]["status"] == ("unverified" if user is None else "shared")


# -- F: staleness of profile and ADR revisions ---------------------------------------------------


def test_profile_pinning_an_outdated_adr_revision_is_stale(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / ADR).write_text(ADR_TEXT.replace("revision: 1", "revision: 2"))
    code, _, err = cli("validate", project)
    assert code == 0 and "ADR_REVISION_MISMATCH" in err
    impl = task_of(live_plan(project, world(project))[0], "SVC-1")
    assert impl["state"] == "STALE"
    assert codes(impl["stale"]) == {"ADR_REVISION_MISMATCH"}


def test_task_pinning_an_outdated_profile_revision_is_stale(project: Path) -> None:
    profile = project / PROFILE
    profile.write_text(profile.read_text().replace("revision: 1\nrepository", "revision: 2\nrepository"))
    impl = task_of(live_plan(project, world(project))[0], "SVC-1")
    assert impl["state"] == "STALE"
    assert codes(impl["stale"]) == {"PROFILE_REVISION_MISMATCH"}


def test_adr_changed_locally_after_its_merge_is_stale(project: Path) -> None:
    responses = world(project)
    (project / ADR).write_text(ADR_TEXT + "\nAmended after the merge.\n")
    impl = task_of(live_plan(project, responses)[0], "SVC-1")
    assert impl["state"] == "STALE"
    assert codes(impl["stale"]) == {"ADR_CHANGED_SINCE_APPROVAL"}


# -- G, H: ADR structure is a validation error, found before any GitHub request ------------------


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(
            lambda p: _adr(p, "critique: [docs/adr/research/ADR-1-critique.md]\n", ""),
            (ADR, "critique", "MISSING_FIELD"),
            id="critique-missing",
        ),
        pytest.param(
            lambda p: _adr(p, "research: [docs/adr/research/ADR-1-options.md]\n", ""),
            (ADR, "research", "MISSING_FIELD"),
            id="research-missing",
        ),
        pytest.param(
            lambda p: _adr(p, "[docs/adr/research/ADR-1-critique.md]", "[]"),
            (ADR, "critique", "TOO_FEW_ITEMS"),
            id="critique-empty",
        ),
        pytest.param(
            lambda p: (p / "docs/adr/research/ADR-1-critique.md").unlink(),
            (ADR, "critique[0]", "REFERENCED_FILE_NOT_FOUND"),
            id="critique-unresolved",
        ),
        pytest.param(
            lambda p: (p / "docs/adr/research/ADR-1-options.md").unlink(),
            (ADR, "research[0]", "REFERENCED_FILE_NOT_FOUND"),
            id="research-unresolved",
        ),
        pytest.param(lambda p: _adr(p, "id: ADR-1\n", ""), (ADR, "id", "MISSING_FIELD"), id="id-missing"),
        pytest.param(lambda p: _adr(p, "revision: 1\n", ""), (ADR, "revision", "MISSING_FIELD"), id="revision-missing"),
        pytest.param(
            lambda p: (p / PROFILE).write_text((p / PROFILE).read_text().replace("id: ADR-1", "id: ADR-9")),
            (PROFILE, "adrs[0].id", "ADR_NOT_FOUND"),
            id="profile-references-unknown-adr",
        ),
    ],
)
def test_malformed_architecture_documents_fail_validation_before_collection(
    project: Path, change: Any, expected: tuple[str, str, str]
) -> None:
    change(project)
    transport = FakeTransport({})
    code, out, _ = invoke("plan", project, "--live", "--format", "json", transport=transport)
    assert code == 2
    assert expected in [(e["file"], e["field"], e["code"]) for e in json.loads(out)["errors"]]
    assert transport.requests == []


def _adr(project: Path, old: str, new: str) -> None:
    text = (project / ADR).read_text()
    assert old in text
    (project / ADR).write_text(text.replace(old, new, 1))


def test_superseded_adr_needs_no_research_or_critique(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    text = ADR_TEXT.replace("status: accepted", "status: superseded")
    text = re.sub(r"(research|critique): \[.*\]\n", "", text)
    (project / "docs/adr/ADR-0-old.md").write_text(text.replace("id: ADR-1", "id: ADR-0"))
    assert cli("validate", project)[0] == 0


# -- I: spikes ------------------------------------------------------------------------------------


def test_spike_without_a_profile_is_valid_and_not_held_to_profile_rules(project: Path) -> None:
    plan, _, _ = live_plan(project, world(project))
    spike = task_of(plan, "SPIKE-1")
    assert spike["architecture"] == {
        "profile": None,
        "profileRevision": None,
        "reasons": [],
        "required": False,
        "result": "PASS",
    }
    # Requirements that do apply to spikes are kept: the task file's own approval...
    assert codes(spike["gates"]["start"]["reasons"]) == {"TASK_NOT_APPROVED", "HUMAN_AUTHORIZATION_UNVERIFIABLE"}


def test_spike_specification_change_still_makes_it_stale(project: Path) -> None:
    responses = world(project)
    (project / "specs/spike-1.md").write_text("# Spike: changed question\n")
    assert codes(task_of(live_plan(project, responses)[0], "SPIKE-1")["stale"]) == {"SPEC_CHANGED"}


def test_implementation_task_without_a_profile_is_still_rejected(project: Path) -> None:
    task = project / TASK
    task.write_text(task.read_text().replace("architecture: {profile: svc-arch, profileRevision: 1}\n", ""))
    transport = FakeTransport({})
    code, out, _ = invoke("plan", project, "--live", "--format", "json", transport=transport)
    assert code == 2
    assert (TASK, "architecture", "MISSING_FIELD") in [
        (e["file"], e["field"], e["code"]) for e in json.loads(out)["errors"]
    ]
    assert transport.requests == []


# -- J, K: collection failures never produce a complete plan --------------------------------------


def test_repository_collection_failure_is_incomplete_and_unknown(project: Path) -> None:
    responses = world(project)
    responses[BASE] = Sequence([Response(502, {"message": "Bad Gateway"}), identity()])
    plan, err, _ = live_plan(project, responses, expect=3)
    assert plan["evidence"]["collection"]["complete"] is False
    # Without a recorded head for the planning repository, approvals cannot be tied to it either.
    assert [(e["fact"], e["message"]) for e in plan["evidence"]["collection"]["errors"]] == [
        ("approvals", APPROVALS_UNANCHORED),
        ("repository", "HTTP 502 Bad Gateway"),
    ]
    assert "incomplete: svc: repository: HTTP 502 Bad Gateway" in err
    assert "2 fact(s) could not be collected" in err
    for task in plan["tasks"]:
        assert task["levels"]["merged"]["result"] == "UNKNOWN"
        assert "PR_EVIDENCE_MISSING" in codes(task["levels"]["merged"]["reasons"])
    assert all_states(plan) == {"UNKNOWN"}


def test_text_output_of_an_incomplete_live_plan_says_so(project: Path) -> None:
    responses = world(project)
    responses[BASE] = Sequence([Response(502, {"message": "Bad Gateway"}), identity()])
    code, out, _ = invoke("plan", project, "--live", transport=FakeTransport(responses))
    assert code == 3
    assert "Collection: INCOMPLETE (2 fact(s) could not be collected and evaluate as UNKNOWN)" in out
    assert "NOT authenticated" in out


def test_total_github_failure_still_fails_closed(project: Path) -> None:
    class Unreachable:
        requests: list[str] = []

        def get(self, path: str) -> Response:
            self.requests.append(path)
            raise GitHubError("gh api failed: error connecting to api.github.com")

    code, out, err = invoke("plan", project, "--live", "--format", "json", transport=Unreachable())
    assert code == 3
    plan = json.loads(out)
    assert plan["evidence"]["collection"]["complete"] is False
    assert plan["evidence"]["provenance"] == "github"
    assert all_states(plan) == {"UNKNOWN"}
    assert "APPROVAL_EVIDENCE_MISSING" in codes(task_of(plan, "SVC-1")["gates"]["start"]["reasons"])


def test_missing_github_cli_writes_no_plan_and_no_fallback(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github.shutil, "which", lambda _name: None)
    target = tmp_path / "saved.json"
    code, out, err = invoke("plan", project, "--live", "--save-evidence", target)
    assert (code, out) == (3, "")
    assert "GitHub CLI (gh) is not installed" in err
    assert not target.exists()


def test_approval_provenance_failure_propagates(project: Path) -> None:
    responses = world(project)
    responses[q(f"{BASE}/contents/{TASK}", ref=HEAD)] = Response(500, {"message": "Server Error"})
    plan, err, _ = live_plan(project, responses, expect=3)
    impl = task_of(plan, "SVC-1")
    assert impl["levels"]["approved"]["result"] == "UNKNOWN"
    assert codes(impl["levels"]["approved"]["reasons"]) == {"APPROVAL_EVIDENCE_MISSING"}
    assert {"fact": f"approvals.{TASK}", "message": "HTTP 500 Server Error", "repository": "svc"} in plan["evidence"][
        "collection"
    ]["errors"]
    assert f"incomplete: svc: approvals.{TASK}: HTTP 500 Server Error" in err


# -- Phase 6: integration heads are confirmed after collection ------------------------------------


@pytest.mark.parametrize(
    ("final", "message"),
    [
        pytest.param(ref(MOVED), f"moved from {HEAD} to {MOVED} after it was collected", id="moved"),
        pytest.param(
            Response(503, {"message": "Unavailable"}),
            "could not be confirmed after collection: HTTP 503 Unavailable",
            id="unreadable",
        ),
    ],
)
def test_head_not_confirmed_after_collection_drops_anchored_facts(project: Path, final: Any, message: str) -> None:
    responses = world(project)
    # Two reads by the repository collector, two by the approval collector, then the final check.
    responses[f"{BASE}/git/ref/heads/dev"] = Sequence([ref(HEAD)] * 4 + [final])
    plan, err, _ = live_plan(project, responses, expect=3)
    errors = plan["evidence"]["collection"]["errors"]
    assert {"fact": "integrationHead", "message": message, "repository": "svc"} in errors
    assert {"fact": "approvals", "message": APPROVALS_UNANCHORED, "repository": "svc"} in errors
    impl = task_of(plan, "SVC-1")
    assert "INTEGRATION_HEAD_UNKNOWN" in codes(impl["levels"]["integration-ready"]["reasons"])
    assert codes(impl["levels"]["approved"]["reasons"]) == {"APPROVAL_EVIDENCE_MISSING"}
    assert f"incomplete: svc: integrationHead: {message}" in err


def _collected_three_repo() -> dict[str, Any]:
    snapshot = three_repo_baseline()
    snapshot["provenance"] = {"kind": "github"}
    snapshot["collection"] = {"collector": "factory 0.1.0", "complete": True, "errors": []}
    snapshot["credentialIsolation"] = {"identity": None, "message": "unverified", "status": "unverified"}
    return snapshot


def _three_repo_live(monkeypatch: pytest.MonkeyPatch, heads: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    snapshot = _collected_three_repo()
    monkeypatch.setattr(live, "collect", lambda *_args, **_kw: CollectionResult(snapshot, []))
    current = {repo: snapshot["repositories"][repo]["integrationHead"] for repo in ("plan", "svc", "client")}
    responses = {
        f"repos/example-org/example-{repo}/git/ref/heads/dev": heads.get(repo, ref(sha))
        for repo, sha in current.items()
    }
    code, out, err = invoke("plan", THREE_REPO, "--live", "--format", "json", transport=FakeTransport(responses))
    return code, json.loads(out)


def test_moved_head_in_another_repository_keeps_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    code, plan = _three_repo_live(monkeypatch, {"svc": ref(MOVED)})
    assert code == 3
    assert [(e["repository"], e["fact"]) for e in plan["evidence"]["collection"]["errors"]] == [
        ("svc", "integrationHead")
    ]
    contract = task_of(plan, "SVC-CONTRACT-002")
    assert "REACHABILITY_UNKNOWN" in codes(contract["levels"]["validated"]["reasons"])
    assert "INTEGRATION_HEAD_UNKNOWN" in codes(contract["levels"]["integration-ready"]["reasons"])
    # The planning repository's head is confirmed, so its approvals still count (as unverifiable).
    assert codes(contract["levels"]["approved"]["reasons"]) == {"HUMAN_AUTHORIZATION_UNVERIFIABLE"}
    # Repositories whose heads were confirmed keep their facts.
    client = task_of(plan, "CLIENT-UI-003")
    assert "INTEGRATION_HEAD_UNKNOWN" not in codes(client["levels"]["integration-ready"]["reasons"])


def test_moved_planning_head_drops_every_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    code, plan = _three_repo_live(monkeypatch, {"plan": ref(MOVED)})
    assert code == 3
    assert {(e["repository"], e["fact"]) for e in plan["evidence"]["collection"]["errors"]} == {
        ("plan", "approvals"),
        ("plan", "integrationHead"),
    }
    for task in plan["tasks"]:
        assert codes(task["levels"]["approved"]["reasons"]) == {"APPROVAL_EVIDENCE_MISSING"}


def test_approvals_without_a_recorded_planning_head_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _collected_three_repo()
    del snapshot["repositories"]["plan"]["integrationHead"]
    snapshot["collection"] = {
        "collector": "factory 0.1.0",
        "complete": False,
        "errors": [{"fact": "integrationHead", "message": "moved during collection", "repository": "plan"}],
    }
    monkeypatch.setattr(live, "collect", lambda *_args, **_kw: CollectionResult(snapshot, []))
    heads = {
        f"repos/example-org/example-{repo}/git/ref/heads/dev": ref(snapshot["repositories"][repo]["integrationHead"])
        for repo in ("svc", "client")
    }
    code, out, _ = invoke("plan", THREE_REPO, "--live", "--format", "json", transport=FakeTransport(heads))
    plan = json.loads(out)
    assert code == 3
    assert ("plan", "approvals") in [(e["repository"], e["fact"]) for e in plan["evidence"]["collection"]["errors"]]
    assert all(codes(t["levels"]["approved"]["reasons"]) == {"APPROVAL_EVIDENCE_MISSING"} for t in plan["tasks"])


# -- L, M: one validation path for files and in-memory snapshots ----------------------------------


def _recorded_snapshot() -> dict[str, Any]:
    from factory.collector import collect
    from factory.github import GitHubApi

    loaded = load_project(SELF_PROJECT).project
    assert loaded is not None
    return collect(loaded, GitHubApi(load_recorded()), now=CLOCK).snapshot


MALFORMED = [
    pytest.param(lambda d: d.pop("approvals"), id="approvals-missing"),
    pytest.param(lambda d: d.pop("schemaVersion"), id="schema-version-missing"),
    pytest.param(lambda d: d["provenance"].update(kind="verified"), id="provenance-verified"),
    pytest.param(lambda d: d["provenance"].pop("kind"), id="provenance-kind-missing"),
    pytest.param(lambda d: d["approvals"][0].update(humanAuthorization="verified"), id="human-authorization-verified"),
    pytest.param(lambda d: d["approvals"][0].update(mergeCommit="HEAD"), id="malformed-merge-commit"),
    pytest.param(lambda d: d["approvals"][0].update(evidence="APPROVED_BY_HUMAN"), id="unknown-evidence-code"),
    pytest.param(lambda d: d["approvals"][0].pop("sha256"), id="approval-hash-missing"),
    pytest.param(lambda d: d["credentialIsolation"].update(status="isolated"), id="credential-isolation-isolated"),
    pytest.param(lambda d: d["collection"].pop("complete"), id="collection-complete-missing"),
    pytest.param(lambda d: d.update(trusted=True), id="trusted-flag"),
    pytest.param(lambda d: d["repositories"]["factory"].update(integrationHead="HEAD"), id="malformed-head"),
    pytest.param(lambda d: d.update(project="someone-else"), id="project-mismatch"),
    pytest.param(lambda d: d["repositories"].update(ghost={}), id="unknown-repository"),
    pytest.param(lambda d: d["approvals"].append(dict(d["approvals"][0])), id="duplicate-approval"),
]


@pytest.mark.parametrize("change", MALFORMED)
def test_in_memory_and_file_snapshots_fail_the_same_checks(tmp_path: Path, change: Any) -> None:
    document = _recorded_snapshot()
    change(document)
    loaded = load_project(SELF_PROJECT).project
    assert loaded is not None
    path = write_snapshot(document, tmp_path / "snapshot.json")
    from_file = load_snapshot(path, loaded)
    in_memory = parse_snapshot(document, loaded, path.as_posix())
    assert from_file.errors
    assert in_memory.errors == from_file.errors
    assert in_memory.snapshot is None and from_file.snapshot is None


@pytest.mark.parametrize("change", MALFORMED)
def test_malformed_live_snapshot_cannot_bypass_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Any
) -> None:
    document = _recorded_snapshot()
    change(document)
    path = write_snapshot(document, tmp_path / "snapshot.json")
    offline = invoke("plan", SELF_PROJECT, "--evidence", path, "--format", "json")
    monkeypatch.setattr(live, "collect", lambda *_args, **_kw: CollectionResult(document, []))
    online = invoke("plan", SELF_PROJECT, "--live", "--format", "json", transport=FakeTransport({}))
    assert online[0] == offline[0] == 2
    as_errors = [{k: v for k, v in e.items() if k != "file"} for e in json.loads(online[1])["errors"]]
    assert as_errors == [{k: v for k, v in e.items() if k != "file"} for e in json.loads(offline[1])["errors"]]
    assert "tasks" not in json.loads(online[1])


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda d: d.update(provenance={"kind": "simulated"}), id="simulated-provenance"),
        pytest.param(lambda d: d.pop("collection"), id="no-collection-record"),
    ],
)
def test_live_planning_refuses_evidence_the_collector_did_not_write(
    monkeypatch: pytest.MonkeyPatch, change: Any
) -> None:
    document = _recorded_snapshot()
    change(document)
    monkeypatch.setattr(live, "collect", lambda *_args, **_kw: CollectionResult(document, []))
    transport = FakeTransport({})
    code, out, _ = invoke("plan", SELF_PROJECT, "--live", "--format", "json", transport=transport)
    assert code == 2
    assert [e["code"] for e in json.loads(out)["errors"]] == ["LIVE_EVIDENCE_NOT_COLLECTED"]
    assert transport.requests == []


# -- A, N, Q: offline planning is unchanged and byte-identical ------------------------------------

GOLDEN = [
    pytest.param(THREE_REPO, "three-repo-baseline", id="three-repo-baseline"),
    pytest.param(THREE_REPO, "three-repo-merged-without-push-ci", id="three-repo-merged-without-push-ci"),
    pytest.param(SINGLE_REPO, "single-repo-baseline", id="single-repo-baseline"),
]


@pytest.mark.parametrize(("project_dir", "name"), GOLDEN)
@pytest.mark.parametrize("fmt", ["json", "text"])
def test_offline_plan_is_byte_identical_to_stage3(project_dir: Path, name: str, fmt: str) -> None:
    """Golden files were written by the Stage 3 merge (72077a0) before any Stage 4 change."""
    golden = (FIXTURES / "plans" / f"{name}.{fmt}.out").read_text()
    for _ in range(2):
        code, out, err = invoke("plan", project_dir, "--evidence", EVIDENCE / f"{name}.json", "--format", fmt)
        assert (code, err) == (0, "")
        assert out == golden


# -- O: evidence source options -------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("plan", "{p}"), id="no-source"),
        pytest.param(("plan", "{p}", "--live", "--evidence", "{s}"), id="both-sources"),
        pytest.param(("plan", "{p}", "--evidence", "{s}", "--save-evidence", "{o}"), id="save-without-live"),
        pytest.param(("plan", "{p}", "--live", "--save-evidence", "{p}/snapshot.json"), id="save-inside-project"),
    ],
)
def test_conflicting_or_incomplete_source_options_are_usage_errors(
    project: Path, tmp_path: Path, args: tuple[str, ...]
) -> None:
    snapshot = write_snapshot({}, tmp_path / "s.json")
    values = {"p": str(project), "s": str(snapshot), "o": str(tmp_path / "o.json")}
    transport = FakeTransport({})
    code, out, _ = invoke(*(a.format(**values) for a in args), transport=transport)
    assert (code, out) == (64, "")
    assert transport.requests == []
    assert not (project / "snapshot.json").exists()


# -- P: read-only -----------------------------------------------------------------------------------


def _fingerprint(root: Path) -> dict[str, tuple[str, int]]:
    return {
        p.relative_to(root).as_posix(): (
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir",
            p.stat().st_mtime_ns,
        )
        for p in sorted(root.rglob("*"))
    }


def test_live_plan_issues_only_get_requests_and_never_writes_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = json.loads(RECORDED.read_text())
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        entry = recorded[command[-1]]
        body = f"HTTP/2.0 {entry['status']} X\r\nContent-Type: application/json\r\n\r\n{json.dumps(entry['body'])}"
        return subprocess.CompletedProcess(command, 0, stdout=body, stderr="")

    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github.subprocess, "run", fake_run)
    project = copy_project(SELF_PROJECT, tmp_path)
    before = _fingerprint(project)
    code, out, _ = invoke("plan", project, "--live", "--save-evidence", tmp_path / "saved.json")
    assert code == 0 and out.startswith("Project: agentic-factory")
    assert _fingerprint(project) == before
    assert commands
    for command in commands:
        assert command[:4] == ["/usr/bin/gh", "api", "--method", "GET"]
        assert not {"-f", "-F", "--field", "--raw-field", "--input", "-X"} & set(command)


# -- R, S: determinism and live timestamps --------------------------------------------------------


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_live_plan_is_byte_identical_for_identical_responses_and_clock(project: Path, fmt: str) -> None:
    outputs = {
        invoke("plan", project, "--live", "--format", fmt, transport=FakeTransport(world(project)))[1] for _ in range(3)
    }
    assert len(outputs) == 1


def test_observed_at_is_the_only_clock_dependent_output(project: Path, tmp_path: Path) -> None:
    later = lambda: datetime(2026, 12, 1, 8, 30, 15, 999, tzinfo=UTC)  # noqa: E731
    first, _, _ = live_plan(project, world(project), "--save-evidence", tmp_path / "a.json")
    second, _, _ = live_plan(project, world(project), "--save-evidence", tmp_path / "b.json", clock=later)
    assert (first["evidence"]["observedAt"], second["evidence"]["observedAt"]) == (
        "2026-11-11T12:00:00Z",
        "2026-12-01T08:30:15Z",
    )
    for document in (first, second):
        del document["evidence"]["observedAt"]
    assert first == second
    snapshots = [json.loads((tmp_path / name).read_text()) for name in ("a.json", "b.json")]
    for document in snapshots:
        del document["observedAt"]
    assert snapshots[0] == snapshots[1]


# -- T, U: no simulated authorization, no dispatch ------------------------------------------------


def test_no_snapshot_or_plan_is_ever_authenticated(project: Path, tmp_path: Path) -> None:
    collected = saved_snapshot(project, world(project), tmp_path)
    ready = offline_plan(project, simulated(collected), tmp_path)
    assert all_states(ready) == {"READY"}
    assert ready["evidence"] == {
        "authenticated": False,
        "observedAt": "2026-11-11T12:00:00Z",
        "provenance": "simulated",
    }
    assert live_plan(project, world(project))[0]["evidence"]["authenticated"] is False


FORBIDDEN_KEYS = {"trusted", "verified", "allowDispatch", "dispatch", "execute", "authorized", "permission"}


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_plan_output_has_no_authorization_or_dispatch_switch(project: Path) -> None:
    assert not FORBIDDEN_KEYS & _keys(live_plan(project, world(project))[0])


def test_no_command_can_dispatch_or_act() -> None:
    parser = cli_module._parser()
    commands = next(a for a in parser._actions if a.dest == "command")
    assert set(commands.choices) == {"validate", "plan", "evidence"}  # type: ignore[union-attr]
    source = Path(cli_module.__file__).parent
    for module in sorted(source.glob("*.py")):
        text = module.read_text()
        if module.name != "github.py":
            assert "subprocess" not in text, module.name
        assert not re.search(r"[\"'](POST|PUT|PATCH|DELETE)[\"']", text), module.name


def test_live_plan_refuses_a_project_with_dispatch_enabled(project: Path) -> None:
    manifest = project / "factory/project.yaml"
    manifest.write_text(manifest.read_text().replace("dispatch: {enabled: false}", "dispatch: {enabled: true}"))
    transport = FakeTransport({})
    code, out, _ = invoke("plan", project, "--live", "--format", "json", transport=transport)
    assert code == 2
    assert [e["code"] for e in json.loads(out)["errors"]] == ["DISPATCH_NOT_SUPPORTED"]
    assert transport.requests == []
