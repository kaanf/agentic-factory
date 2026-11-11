"""Approval provenance from merge records (Stage 3), judged end to end by the planner.

Every scenario uses mocked GitHub responses. The central rule under test: GitHub provenance may be
recorded, but a merge by an approver's login is never treated as human approval.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import codes
from github_support import (
    BASE,
    REPO,
    FakeTransport,
    Sequence,
    commit_entry,
    contents,
    identity,
    listing,
    not_found,
    page,
    pull,
    pull_detail,
    q,
    ref,
)
from support import THREE_REPO

from factory.approvals import approval_paths
from factory.cli import run
from factory.collector import collect
from factory.evidence import load_snapshot
from factory.github import GitHubApi, Response
from factory.planning import evaluate
from factory.project import load_project


def sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()  # noqa: S324 - fake commit ids


HEAD = sha("dev head")
MERGE = sha("merge commit of #7")
FEATURE = sha("second commit on the feature branch")
PARENT = sha("dev before #7")
LATER = sha("direct push after #7")
PROFILE_COMMIT = sha("profile commit")
PROFILE_MERGE = sha("merge commit of #6")
CLOCK = lambda: datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # noqa: E731
TASK = "factory/tasks/SVC-1.yaml"
PROFILE = "factory/profiles/svc.yaml"
BRANCH = "feat/SVC-1-r1"
PROFILE_TEXT = "schemaVersion: factory.profile/v1\nid: svc-arch\nrevision: 1\nrepository: svc\nadrs: []\n"


def _task_text(spec_digest: str) -> str:
    return f"""schemaVersion: factory.task/v1
id: SVC-1
revision: 1
title: Stock endpoint
repository: svc
kind: implementation
specification: {{path: specs/svc-1.md, sha256: {spec_digest}}}
architecture: {{profile: svc-arch, profileRevision: 1}}
acceptance: [GET /items works]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "svc-project"
    for directory in ("factory/tasks", "factory/profiles", "specs"):
        (root / directory).mkdir(parents=True)
    (root / "factory/project.yaml").write_text(
        f"""schemaVersion: factory.project/v1
project: {{id: svc-project}}
approvers: [example-human]
planningRepository: svc
repositories:
  - id: svc
    github: {REPO}
    branches: {{integration: dev, release: main, featurePrefix: feat/}}
    requiredChecks:
      integration:
        - {{name: "Svc Check", workflowPath: .github/workflows/ci.yml}}
architecture:
  adrDirectory: docs/adr
  profiles:
    - {{id: svc-arch, repository: svc, path: {PROFILE}}}
dispatch: {{enabled: false}}
"""
    )
    (root / PROFILE).write_text(PROFILE_TEXT)
    (root / "specs/svc-1.md").write_text("# Stock endpoint\n")
    (root / TASK).write_text(_task_text(hashlib.sha256(b"# Stock endpoint\n").hexdigest()))
    return root


def text_of(project: Path, path: str) -> str:
    return (project / path).read_text()


# -- mocked GitHub ----------------------------------------------------------------------------


def base_world(head: str = HEAD, user: Any = None) -> dict[str, Any]:
    """Repository facts for an empty task branch, plus identity and head for approvals."""
    return {
        BASE: identity(),
        f"{BASE}/git/ref/heads/dev": ref(head),
        page(f"{BASE}/pulls", head=f"example-org:{BRANCH}", state="all"): [],
        page(f"{BASE}/commits/{head}/pulls"): [],
        page(f"{BASE}/commits/{head}/check-runs", filter="all"): listing("check_runs", []),
        q(f"{BASE}/compare/{head}...{head}"): {"status": "identical"},
        "user": Response(401, {"message": "Requires authentication"}) if user is None else user,
    }


def artifact(
    path: str,
    text: str,
    *,
    head: str = HEAD,
    commit: str = FEATURE,
    number: int = 7,
    merge: str = MERGE,
    associated: list[dict[str, Any]] | None = None,
    detail: dict[str, Any] | None = None,
    merge_text: str | None = None,
    parent_text: str | None = None,
    compare: str = "ahead",
) -> dict[str, Any]:
    """Responses for a file whose content at ``head`` was last changed by ``commit``, part of PR #``number``."""
    return {
        q(f"{BASE}/contents/{path}", ref=head): contents(text),
        q(f"{BASE}/commits", path=path, per_page=1, sha=head): [commit_entry(commit)],
        page(f"{BASE}/commits/{commit}/pulls"): [pull(number, BRANCH, merged_sha=merge)]
        if associated is None
        else associated,
        f"{BASE}/pulls/{number}": pull_detail(number, BRANCH, merged_sha=merge) if detail is None else detail,
        q(f"{BASE}/compare/{merge}...{head}"): {"status": compare},
        q(f"{BASE}/contents/{path}", ref=merge): contents(text if merge_text is None else merge_text),
        f"{BASE}/commits/{merge}": commit_entry(merge, [PARENT]),
        q(f"{BASE}/contents/{path}", ref=PARENT): not_found() if parent_text is None else contents(parent_text),
    }


def approved_world(project: Path, **task_overrides: Any) -> dict[str, Any]:
    """Both artifacts introduced by PR #7, merged by the approver; ``task_overrides`` change the task file's records."""
    responses = base_world(user=task_overrides.pop("user", None))
    responses.update(artifact(PROFILE, PROFILE_TEXT, commit=PROFILE_COMMIT, number=6, merge=PROFILE_MERGE))
    responses.update(artifact(TASK, text_of(project, TASK), **task_overrides))
    return responses


def collected(project: Path, responses: dict[str, Any]) -> tuple[dict[str, Any], FakeTransport]:
    loaded = load_project(project).project
    assert loaded is not None
    transport = FakeTransport(responses)
    return collect(loaded, GitHubApi(transport), now=CLOCK).snapshot, transport


def entry(snapshot: dict[str, Any], path: str = TASK) -> dict[str, Any] | None:
    return next((a for a in snapshot["approvals"] if a["path"] == path), None)


def planned(project: Path, snapshot: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    loaded = load_project(project).project
    assert loaded is not None
    file = tmp_path / "snapshot.json"
    file.write_text(json.dumps(snapshot))
    result = load_snapshot(file, loaded)
    assert result.errors == [], result.errors
    assert result.snapshot is not None
    return evaluate(loaded, result.snapshot).tasks[0].to_dict()


def approval_reasons(task: dict[str, Any]) -> tuple[str, set[str]]:
    level = task["levels"]["approved"]
    return level["result"], codes(level["reasons"])


# -- valid provenance is recorded, but never counts as human approval ---------------------------


@pytest.mark.parametrize(
    ("commit", "merge"),
    [
        pytest.param(FEATURE, MERGE, id="merge-commit-over-several-feature-commits"),
        pytest.param(MERGE, MERGE, id="squash-merge"),
    ],
)
def test_artifact_introduced_by_merged_pr_records_provenance_only(
    project: Path, tmp_path: Path, commit: str, merge: str
) -> None:
    responses = base_world()
    responses.update(artifact(PROFILE, PROFILE_TEXT, commit=commit, merge=merge))
    responses.update(artifact(TASK, text_of(project, TASK), commit=commit, merge=merge))
    snapshot, _ = collected(project, responses)
    assert entry(snapshot) == {
        "approved": True,
        "commit": commit,
        "evidence": "MERGED_PULL_REQUEST",
        "humanAuthorization": "unverifiable",
        "mergeCommit": merge,
        "mergedBy": "example-human",
        "path": TASK,
        "pullRequest": 7,
        "sha256": hashlib.sha256((project / TASK).read_bytes()).hexdigest(),
    }
    assert snapshot["collection"]["complete"] is True
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("UNKNOWN", {"HUMAN_AUTHORIZATION_UNVERIFIABLE"})
    assert task["state"] == "UNKNOWN"


def test_unverifiable_approval_never_makes_an_implementation_task_ready(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project, user={"login": "example-human"}))
    task = planned(project, snapshot, tmp_path)
    assert task["state"] != "READY"
    for gate in ("start", "openPullRequest", "merge"):
        assert task["gates"][gate]["result"] == "UNKNOWN"
        assert "HUMAN_AUTHORIZATION_UNVERIFIABLE" in codes(task["gates"][gate]["reasons"])
    # The architecture profile is held to the same rule.
    assert "PROFILE_NOT_APPROVED" in codes(task["gates"]["start"]["reasons"])


def test_same_facts_with_simulated_approvals_are_ready(project: Path, tmp_path: Path) -> None:
    """Control: the only thing keeping the task from READY above is the trust boundary."""
    snapshot, _ = collected(project, approved_world(project))
    simulated = {
        **snapshot,
        "provenance": {"kind": "simulated"},
        "approvals": [
            {k: v for k, v in a.items() if k not in ("humanAuthorization", "evidence")} for a in snapshot["approvals"]
        ],
    }
    assert planned(project, simulated, tmp_path)["state"] == "READY"


# -- credential isolation ----------------------------------------------------------------------


def test_shared_human_and_agent_identity_is_detected(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project, user={"login": "example-human"}))
    isolation = snapshot["credentialIsolation"]
    assert isolation["status"] == "shared"
    assert isolation["identity"] == "example-human"
    assert "credentials are shared" in isolation["message"]
    assert entry(snapshot)["humanAuthorization"] == "unverifiable"


@pytest.mark.parametrize(
    ("user", "identity_login"),
    [
        pytest.param(Response(401, {"message": "Requires authentication"}), None, id="identity-unreadable"),
        pytest.param({"login": "factory-bot"}, "factory-bot", id="non-approver-identity"),
    ],
)
def test_missing_credential_isolation_evidence_stays_unverifiable(
    project: Path, tmp_path: Path, user: Any, identity_login: str | None
) -> None:
    """A non-approver collector identity is not evidence of isolation: agents may hold other credentials."""
    snapshot, _ = collected(project, approved_world(project, user=user))
    assert snapshot["credentialIsolation"]["status"] == "unverified"
    assert snapshot["credentialIsolation"]["identity"] == identity_login
    assert approval_reasons(planned(project, snapshot, tmp_path))[0] == "UNKNOWN"


def test_collected_snapshot_with_the_marker_removed_is_still_unverifiable(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project))
    for approval in snapshot["approvals"]:
        del approval["humanAuthorization"]
    assert "HUMAN_AUTHORIZATION_UNVERIFIABLE" in approval_reasons(planned(project, snapshot, tmp_path))[1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("humanAuthorization", "verified"),
        ("credentialIsolation", {"status": "isolated", "identity": "factory-bot", "message": ""}),
    ],
)
def test_snapshot_schema_has_no_verified_human_authorization(
    project: Path, tmp_path: Path, field: str, value: Any
) -> None:
    snapshot, _ = collected(project, approved_world(project))
    if field == "humanAuthorization":
        snapshot["approvals"][0][field] = value
    else:
        snapshot[field] = value
    file = tmp_path / "forged.json"
    file.write_text(json.dumps(snapshot))
    loaded = load_project(project).project
    assert loaded is not None
    assert load_snapshot(file, loaded).errors


# -- definite negative findings: written with approved=false, the task is BLOCKED --------------


def test_wrong_merge_actor(project: Path, tmp_path: Path) -> None:
    detail = pull_detail(7, BRANCH, merged_sha=MERGE, merged_by="factory-bot")
    snapshot, _ = collected(project, approved_world(project, detail=detail))
    assert entry(snapshot)["mergedBy"] == "factory-bot"
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("FAIL", {"APPROVER_NOT_AUTHORIZED"})
    assert task["state"] == "BLOCKED"


@pytest.mark.parametrize(
    ("associated", "evidence"),
    [
        pytest.param([], "NO_PULL_REQUEST", id="direct-push-without-pr"),
        pytest.param([pull(7, BRANCH, state="open")], "PR_NOT_MERGED", id="unmerged-pr"),
        pytest.param([pull(7, BRANCH, state="closed")], "PR_NOT_MERGED", id="closed-unmerged-pr"),
        pytest.param(
            [pull(7, BRANCH, base="main", merged_sha=MERGE)], "PR_NOT_INTO_INTEGRATION_BRANCH", id="wrong-base"
        ),
    ],
)
def test_content_not_introduced_by_a_merged_pr_into_dev(
    project: Path, tmp_path: Path, associated: list[dict[str, Any]], evidence: str
) -> None:
    snapshot, transport = collected(project, approved_world(project, associated=associated))
    approval = entry(snapshot)
    assert approval is not None
    assert (approval["approved"], approval["evidence"]) == (False, evidence)
    assert "mergedBy" not in approval
    assert f"{BASE}/pulls/7" not in transport.requests
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("FAIL", {"NOT_APPROVED"})
    assert task["state"] == "BLOCKED"


def test_artifact_modified_on_dev_after_approval_by_direct_push(project: Path, tmp_path: Path) -> None:
    """The approving PR is no longer the latest change: the direct push that followed is."""
    snapshot, _ = collected(project, approved_world(project, commit=LATER, associated=[]))
    assert (entry(snapshot)["commit"], entry(snapshot)["evidence"]) == (LATER, "NO_PULL_REQUEST")


def test_content_already_on_dev_before_the_pr_was_merged(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project, parent_text=text_of(project, TASK)))
    approval = entry(snapshot)
    assert (approval["approved"], approval["evidence"], approval["pullRequest"]) == (
        False,
        "CONTENT_PRESENT_BEFORE_MERGE",
        7,
    )


def test_artifact_not_on_the_integration_branch(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{TASK}", ref=HEAD)] = not_found()
    snapshot, _ = collected(project, responses)
    approval = entry(snapshot)
    assert (approval["approved"], approval["evidence"]) == (False, "NOT_ON_INTEGRATION_BRANCH")
    assert approval["sha256"] == hashlib.sha256((project / TASK).read_bytes()).hexdigest()


# -- staleness ----------------------------------------------------------------------------------


def test_local_artifact_modified_after_approval_is_stale(project: Path, tmp_path: Path) -> None:
    approved_text = text_of(project, TASK)
    responses = approved_world(project)
    (project / TASK).write_text(approved_text.replace("GET /items works", "GET /items and /stock work"))
    snapshot, _ = collected(project, responses)
    assert entry(snapshot)["sha256"] == hashlib.sha256(approved_text.encode()).hexdigest()
    task = planned(project, snapshot, tmp_path)
    assert task["state"] == "STALE"
    assert "TASK_CHANGED_SINCE_APPROVAL" in codes(task["stale"])


def test_invalid_artifact_hash_in_snapshot_is_rejected(project: Path, tmp_path: Path) -> None:
    snapshot, _ = collected(project, approved_world(project))
    snapshot["approvals"][0]["sha256"] = "not-a-hash"
    file = tmp_path / "bad.json"
    file.write_text(json.dumps(snapshot))
    loaded = load_project(project).project
    assert loaded is not None
    assert [e.code for e in load_snapshot(file, loaded).errors] == ["INVALID_FORMAT"]


# -- missing, contradictory or ambiguous evidence: omitted, the task is UNKNOWN ----------------


def _no_merge_commit() -> dict[str, Any]:
    detail = pull_detail(7, BRANCH, merged_sha=MERGE)
    detail["merge_commit_sha"] = None
    return detail


def _contradictory_detail() -> dict[str, Any]:
    detail = pull_detail(7, BRANCH, merged_sha=None)
    return detail


def _blob_mismatch(project: Path) -> dict[str, Any]:
    body = contents(text_of(project, TASK))
    body["sha"] = "0" * 40
    return body


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            {"detail": pull_detail(7, BRANCH, merged_sha=MERGE, merged_by=None)},
            "no recorded merge commit or merge actor",
            id="missing-merge-actor",
        ),
        pytest.param(
            {"detail": _no_merge_commit()}, "no recorded merge commit or merge actor", id="missing-merge-commit"
        ),
        pytest.param(
            {"associated": [pull(7, BRANCH, merged_sha=MERGE), pull(8, "feat/other-r1", merged_sha=sha("m8"))]},
            "several merged pull requests (#7, #8)",
            id="ambiguous-pr-association",
        ),
        pytest.param(
            {"detail": _contradictory_detail()},
            "listed as merged into the integration branch, but is not",
            id="list-and-detail-disagree",
        ),
        pytest.param(
            {"merge_text": "something else\n"}, "differs from the integration head", id="merge-content-differs"
        ),
        pytest.param({"compare": "diverged"}, "not reachable from the integration head", id="merge-not-on-dev"),
    ],
)
def test_incomplete_or_contradictory_records_are_omitted(
    project: Path, tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    snapshot, _ = collected(project, approved_world(project, **change))
    assert entry(snapshot) is None
    assert entry(snapshot, PROFILE) is not None  # only the affected artifact is dropped
    errors = snapshot["collection"]["errors"]
    assert [e["fact"] for e in errors] == [f"approvals.{TASK}"]
    assert message in errors[0]["message"]
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("UNKNOWN", {"APPROVAL_EVIDENCE_MISSING"})
    assert task["state"] == "UNKNOWN"


def test_content_not_matching_its_blob_sha_is_omitted(project: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{TASK}", ref=HEAD)] = _blob_mismatch(project)
    snapshot, _ = collected(project, responses)
    assert entry(snapshot) is None
    assert "does not match its blob SHA" in snapshot["collection"]["errors"][0]["message"]


def test_no_commit_recorded_for_a_file_on_dev_is_omitted(project: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/commits", path=TASK, per_page=1, sha=HEAD)] = []
    snapshot, _ = collected(project, responses)
    assert entry(snapshot) is None


def test_integration_head_moving_drops_every_approval(project: Path) -> None:
    moved = sha("dev moved")
    responses = approved_world(project)
    # Two reads by the repository collector, then before/after reads by the approval collector.
    responses[f"{BASE}/git/ref/heads/dev"] = Sequence([ref(HEAD), ref(HEAD), ref(HEAD), ref(moved)])
    snapshot, _ = collected(project, responses)
    assert snapshot["approvals"] == []
    assert {
        "fact": "approvals",
        "message": f"integration head moved from {HEAD} to {moved} during collection",
        "repository": "svc",
    } in snapshot["collection"]["errors"]


# -- malformed records: never positive provenance, never an unjustified definite rejection -----


def _listed(**fields: Any) -> list[dict[str, Any]]:
    """PR #7 as the commit's associated-PR listing returns it, with ``fields`` replaced."""
    return [{**pull(7, BRANCH, merged_sha=MERGE), **fields}]


def _listed_without(field: str) -> list[dict[str, Any]]:
    record = pull(7, BRANCH, merged_sha=MERGE)
    del record[field]
    return [record]


def _detail(**fields: Any) -> dict[str, Any]:
    """PR #7's detail record, with ``fields`` replaced."""
    return {**pull_detail(7, BRANCH, merged_sha=MERGE), **fields}


def _detail_without(field: str) -> dict[str, Any]:
    record = pull_detail(7, BRANCH, merged_sha=MERGE)
    del record[field]
    return record


def assert_omitted(project: Path, tmp_path: Path, snapshot: dict[str, Any], message: str) -> None:
    assert entry(snapshot) is None
    assert entry(snapshot, PROFILE) is not None  # only the affected artifact is dropped
    errors = snapshot["collection"]["errors"]
    assert [e["fact"] for e in errors] == [f"approvals.{TASK}"]
    assert message in errors[0]["message"]
    assert snapshot["collection"]["complete"] is False
    task = planned(project, snapshot, tmp_path)
    assert approval_reasons(task) == ("UNKNOWN", {"APPROVAL_EVIDENCE_MISSING"})
    assert task["state"] == "UNKNOWN"


NO_FIRST_PARENT = "has no valid first parent"


@pytest.mark.parametrize(
    ("record", "message"),
    [
        pytest.param({"parents": None, "sha": MERGE}, NO_FIRST_PARENT, id="parents-null"),
        pytest.param({"parents": {}, "sha": MERGE}, NO_FIRST_PARENT, id="parents-empty-object"),
        pytest.param({"parents": False, "sha": MERGE}, NO_FIRST_PARENT, id="parents-false"),
        pytest.param({"parents": [], "sha": MERGE}, NO_FIRST_PARENT, id="parents-empty-array"),
        pytest.param({"sha": MERGE}, NO_FIRST_PARENT, id="parents-missing"),
        pytest.param({"parents": [PARENT], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-not-a-record"),
        pytest.param({"parents": [{"url": "x"}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-without-sha"),
        pytest.param({"parents": [{"sha": "HEAD~1"}], "sha": MERGE}, NO_FIRST_PARENT, id="first-parent-sha-malformed"),
        pytest.param({"parents": [{"sha": PARENT}], "sha": FEATURE}, "describes another commit", id="other-commit"),
        pytest.param({"parents": [{"sha": PARENT}]}, "describes another commit", id="commit-sha-missing"),
        pytest.param([], "describes another commit", id="not-a-commit-record"),
    ],
)
def test_merge_commit_without_a_valid_first_parent_is_omitted(
    project: Path, tmp_path: Path, record: Any, message: str
) -> None:
    """Without the first parent, nothing shows that this PR introduced the content."""
    responses = approved_world(project)
    responses[f"{BASE}/commits/{MERGE}"] = record
    snapshot, _ = collected(project, responses)
    assert_omitted(project, tmp_path, snapshot, message)


def test_valid_two_parent_merge_commit_is_checked_against_its_first_parent(project: Path) -> None:
    responses = approved_world(project)
    responses[f"{BASE}/commits/{MERGE}"] = commit_entry(MERGE, [PARENT, FEATURE])
    snapshot, transport = collected(project, responses)
    assert (entry(snapshot)["approved"], entry(snapshot)["evidence"]) == (True, "MERGED_PULL_REQUEST")
    assert q(f"{BASE}/contents/{TASK}", ref=PARENT) in transport.requests
    assert snapshot["collection"]["complete"] is True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param({"detail": _detail(number=999)}, "GitHub returned #999", id="detail-wrong-number"),
        pytest.param(
            {"detail": pull_detail(7, BRANCH, merged_sha=MERGE, base_repo="other/repo")},
            "targets repository 'other/repo'",
            id="detail-wrong-base-repository",
        ),
        pytest.param(
            {"associated": [pull(7, BRANCH, merged_sha=MERGE, base_repo="other/repo")]},
            "targets repository 'other/repo'",
            id="listing-wrong-base-repository",
        ),
        pytest.param(
            {"detail": pull_detail(7, BRANCH, merged_sha=MERGE, base="main")},
            "listed as merged into the integration branch, but is not",
            id="detail-wrong-base-branch",
        ),
        pytest.param(
            {"detail": _detail(merge_commit_sha=sha("another merge"))},
            "two different merge commits",
            id="listing-and-detail-merge-commits-differ",
        ),
        pytest.param(
            {"detail": _detail(merge_commit_sha="not-a-sha")},
            "no recorded merge commit or merge actor",
            id="detail-merge-commit-malformed",
        ),
        pytest.param(
            {"detail": _detail(merged_by={"login": 42})},
            "no recorded merge commit or merge actor",
            id="detail-merge-actor-malformed",
        ),
        pytest.param({"detail": _detail_without("number")}, "no valid number", id="detail-number-missing"),
        pytest.param({"detail": _detail(number="7")}, "no valid number", id="detail-number-not-an-integer"),
        pytest.param({"detail": _detail_without("base")}, "no valid number", id="detail-base-missing"),
        pytest.param(
            {"detail": _detail(base={"ref": "dev"})}, "targets repository None", id="detail-base-repository-missing"
        ),
        pytest.param(
            {"associated": _listed(base={"ref": "dev"})},
            "targets repository None",
            id="listing-base-repository-missing",
        ),
        pytest.param({"associated": _listed_without("number")}, "no valid number", id="listing-number-missing"),
        pytest.param({"associated": [None]}, "not a pull request record", id="listing-record-not-an-object"),
    ],
)
def test_contradictory_or_incomplete_pull_request_identity_is_omitted(
    project: Path, tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    snapshot, _ = collected(project, approved_world(project, **change))
    assert_omitted(project, tmp_path, snapshot, message)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param({"associated": _listed_without("merged_at")}, "no merge status", id="listing-merged-at-missing"),
        pytest.param(
            {"associated": _listed(merged_at="yesterday")}, "malformed merge time", id="listing-merged-at-malformed"
        ),
        pytest.param({"associated": _listed(merged_at="")}, "malformed merge time", id="listing-merged-at-empty"),
        pytest.param(
            {"associated": _listed(merged_at=1762844400)}, "malformed merge time", id="listing-merged-at-number"
        ),
        pytest.param({"associated": _listed(merged_at=False)}, "malformed merge time", id="listing-merged-at-false"),
        pytest.param(
            {"associated": [{**pull(7, BRANCH), "state": None}]}, "no valid state", id="unmerged-listing-state-missing"
        ),
        pytest.param({"associated": _listed(state="open")}, "contradictory merge status", id="open-but-merged"),
        pytest.param(
            {"associated": _listed(merged=False)}, "contradictory merge status", id="listing-merged-flag-disagrees"
        ),
        pytest.param(
            {"associated": [{**pull(7, BRANCH), "merged": True}]},
            "contradictory merge status",
            id="merged-flag-without-merge-time",
        ),
        pytest.param(
            {"detail": _detail(merged_at=None)},
            "contradictory merge status",
            id="detail-merged-without-merge-time",
        ),
        pytest.param(
            {"detail": _detail(merged_at="2026-11-11T08:00:00Z")},
            "two different merge times",
            id="listing-and-detail-merge-times-differ",
        ),
    ],
)
def test_missing_or_contradictory_merge_metadata_is_unknown_not_blocked(
    project: Path, tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    snapshot, _ = collected(project, approved_world(project, **change))
    assert_omitted(project, tmp_path, snapshot, message)


def test_explicitly_unmerged_pr_alongside_unknown_merge_status_is_not_a_definite_rejection(
    project: Path, tmp_path: Path
) -> None:
    """One genuinely unmerged PR does not settle the question while another's status is unknown."""
    unknown = pull(8, "feat/other-r1", merged_sha=sha("m8"))
    del unknown["merged_at"]
    snapshot, _ = collected(project, approved_world(project, associated=[pull(7, BRANCH), unknown]))
    assert_omitted(project, tmp_path, snapshot, "pull request #8 has no merge status")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"detail": _detail(number=999)}, id="pr-identity"),
        pytest.param({"associated": _listed_without("merged_at")}, id="merge-metadata"),
    ],
)
def test_cli_reports_malformed_records_with_exit_3(project: Path, tmp_path: Path, change: dict[str, Any]) -> None:
    responses = approved_world(project, **change)
    out, err = io.StringIO(), io.StringIO()
    output = tmp_path / "s.json"
    code = run(
        ["evidence", "collect", str(project), "-o", str(output)], out=out, err=err, transport=FakeTransport(responses)
    )
    assert code == 3
    assert f"incomplete: svc: approvals.{TASK}:" in err.getvalue()
    assert entry(json.loads(output.read_text())) is None


def test_cli_reports_missing_parent_records_with_exit_3(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[f"{BASE}/commits/{MERGE}"] = {"parents": None, "sha": MERGE}
    out, err = io.StringIO(), io.StringIO()
    output = tmp_path / "s.json"
    code = run(
        ["evidence", "collect", str(project), "-o", str(output)], out=out, err=err, transport=FakeTransport(responses)
    )
    assert code == 3
    assert entry(json.loads(output.read_text())) is None


# -- API failures ---------------------------------------------------------------------------------


def test_server_error_omits_the_affected_artifact(project: Path) -> None:
    responses = approved_world(project)
    responses[page(f"{BASE}/commits/{FEATURE}/pulls")] = Response(502, {"message": "Bad Gateway"})
    snapshot, _ = collected(project, responses)
    assert entry(snapshot) is None
    assert entry(snapshot, PROFILE) is not None
    assert snapshot["collection"]["errors"] == [
        {"fact": f"approvals.{TASK}", "message": "HTTP 502 Bad Gateway", "repository": "svc"}
    ]


def test_rate_limit_stops_approval_collection(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project)
    responses[q(f"{BASE}/contents/{PROFILE}", ref=HEAD)] = Response(429, {"message": "Too Many Requests"})
    snapshot, transport = collected(project, responses)
    assert snapshot["approvals"] == []
    assert snapshot["collection"]["complete"] is False
    assert not any(TASK in r for r in transport.requests)  # nothing requested after the limit
    assert planned(project, snapshot, tmp_path)["state"] == "UNKNOWN"


def test_cli_reports_incomplete_approvals_with_exit_3(project: Path, tmp_path: Path) -> None:
    responses = approved_world(project, user={"login": "example-human"})
    responses[f"{BASE}/pulls/7"] = Response(500, {"message": "Server Error"})
    out, err = io.StringIO(), io.StringIO()
    code = run(
        ["evidence", "collect", str(project), "-o", str(tmp_path / "s.json")],
        out=out,
        err=err,
        transport=FakeTransport(responses),
    )
    assert code == 3
    assert (
        "warning: human authorization unverifiable: the GitHub identity used for collection (example-human)"
        in err.getvalue()
    )
    assert f"incomplete: svc: approvals.{TASK}: HTTP 500 Server Error" in err.getvalue()


# -- scope and safety ---------------------------------------------------------------------------


def test_only_get_requests_to_the_planning_repository_and_user(project: Path) -> None:
    _, transport = collected(project, approved_world(project))
    assert all(r.startswith(f"{BASE}") or r == "user" for r in transport.requests)
    assert not any(r.startswith(f"{BASE}/pulls/") and r.endswith(("/merge", "/reviews")) for r in transport.requests)


def test_tasks_profiles_and_adrs_are_all_verified() -> None:
    loaded = load_project(THREE_REPO).project
    assert loaded is not None
    paths = approval_paths(loaded)
    assert "docs/adr/ADR-0001-contract-source.md" in paths
    assert "factory/profiles/svc.yaml" in paths
    assert "factory/tasks/SVC-IMPL-007.yaml" in paths
    assert not any(p.startswith("specs/") for p in paths)
