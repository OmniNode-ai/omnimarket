# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A DoD verdict names the contract it was evaluated against (OMN-20696).

Split from OR.3 (OMN-20071) of the OCC retirement, plan step S4: the Done gate takes a ticket's
verdict from the repository its PR merged into, and reads it through the
projection access node rather than from OCC. Before this change the durable
verdict row in ``dod_verify_runs`` could not answer the one question that read
asks -- was this verdict produced from the product repository's own contract,
at which commit -- because the verifier recorded the OCC ref it read and
nothing at all about a contract read from anywhere else.

The verifier now resolves, at the moment it loads the contract file, where
that file came from: which repository, which commit, which repository-relative
path, and whether the file read is exactly that commit's content. The answer
rides on the state the runtime publishes, through the wire model and the pure
fold, onto the row. Unset, it is omitted from every payload, so an older
consumer sees the shape it saw before.

The rebuild tests at the end are the "rebuildable from events" half: the fold
is a function of the event alone, so replaying the topic in any order, with
redeliveries, reproduces the same set of rows.
"""

from __future__ import annotations

import os
import random
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from pydantic import ValidationError

from omnimarket.enums.enum_dod_contract_source import EnumDodContractSource
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_contract_subject import (
    ModelDodContractSubject,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.contract_subject import (
    github_repository_from_remote,
    resolve_contract_subject,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers import (
    handler_dod_verdict_runner as writer_module,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    DodVerdictProjectionWriter,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    HandlerProjectionDodVerdict,
)
from omnimarket.nodes.node_projection_dod_verdict.models import (
    ModelDodVerdictProjectionRequest,
    ModelDodVerdictRow,
    ModelDodVerdictWire,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_PROJECTION = _ROOT / "src/omnimarket/nodes/node_projection_dod_verdict"
_TICKET = "OMN-20696"
_CONTRACT_REL = "contracts/OMN-20696.yaml"
_SUBJECT_FIELDS = (
    "contract_source",
    "contract_repository",
    "contract_commit_sha",
    "contract_repo_path",
)


# ---------------------------------------------------------------------------
# A real git checkout, so the resolver is exercised against git itself
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return proc.stdout.strip()


def _checkout(tmp_path: Path, remote: str, name: str = "checkout") -> Path:
    """A committed checkout holding one ticket contract, with ``origin`` set."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "lane@example.invalid")
    _git(repo, "config", "user.name", "lane")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", remote)
    contract = repo / _CONTRACT_REL
    contract.parent.mkdir(parents=True)
    contract.write_text(
        yaml.safe_dump({"ticket_id": _TICKET, "title": "t", "dod_evidence": []})
    )
    _git(repo, "add", _CONTRACT_REL)
    _git(repo, "commit", "-q", "-m", "contract")
    return repo


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def test_a_clean_product_checkout_binds_repository_commit_and_path(
    tmp_path: Path,
) -> None:
    repo = _checkout(tmp_path, "https://github.com/OmniNode-ai/omnimarket.git")
    subject = resolve_contract_subject(repo / _CONTRACT_REL)

    assert subject.source is EnumDodContractSource.PRODUCT_REPOSITORY
    assert subject.repository == "OmniNode-ai/omnimarket"
    assert subject.commit_sha == _git(repo, "rev-parse", "HEAD")
    assert subject.repo_path == _CONTRACT_REL


def test_an_inherited_git_dir_does_not_retarget_the_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verification started from a git hook must still name ITS checkout."""
    repo = _checkout(tmp_path, "https://github.com/OmniNode-ai/omnimarket.git")
    other = _checkout(
        tmp_path, "https://github.com/OmniNode-ai/omnibase_infra.git", name="other"
    )
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    subject = resolve_contract_subject(repo / _CONTRACT_REL)

    assert subject.repository == "OmniNode-ai/omnimarket"
    assert subject.commit_sha == _git(repo, "rev-parse", "HEAD")


def test_a_change_control_checkout_is_named_as_such(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, "git@github.com:OmniNode-ai/onex_change_control.git")
    subject = resolve_contract_subject(repo / _CONTRACT_REL)

    assert subject.source is EnumDodContractSource.ONEX_CHANGE_CONTROL
    assert subject.repository == "OmniNode-ai/onex_change_control"
    assert subject.commit_sha == _git(repo, "rev-parse", "HEAD")


def test_a_modified_contract_is_unbound_and_claims_no_commit(tmp_path: Path) -> None:
    """The content read is in no commit, so no commit may be named for it."""
    repo = _checkout(tmp_path, "https://github.com/OmniNode-ai/omnimarket")
    (repo / _CONTRACT_REL).write_text("ticket_id: OMN-20696\ndod_evidence: []\n")
    subject = resolve_contract_subject(repo / _CONTRACT_REL)

    assert subject.source is EnumDodContractSource.UNBOUND
    assert subject.commit_sha is None
    assert subject.repository == "OmniNode-ai/omnimarket"


def test_an_untracked_contract_is_unbound(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, "https://github.com/OmniNode-ai/omnimarket")
    extra = repo / "contracts/OMN-1.yaml"
    extra.write_text("ticket_id: OMN-1\n")
    subject = resolve_contract_subject(extra)

    assert subject.source is EnumDodContractSource.UNBOUND
    assert subject.commit_sha is None


def test_a_contract_outside_any_checkout_is_unbound(tmp_path: Path) -> None:
    loose = tmp_path / "loose.yaml"
    loose.write_text("ticket_id: OMN-1\n")
    subject = resolve_contract_subject(loose)

    assert subject.source is EnumDodContractSource.UNBOUND
    assert subject.repository is None
    assert subject.commit_sha is None
    assert subject.repo_path is None


def test_a_checkout_whose_origin_is_not_github_is_unbound(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, str(tmp_path / "some-local-mirror.git"))
    subject = resolve_contract_subject(repo / _CONTRACT_REL)

    assert subject.source is EnumDodContractSource.UNBOUND
    assert subject.repository is None
    assert subject.commit_sha is None


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/OmniNode-ai/omnimarket.git", "OmniNode-ai/omnimarket"),
        ("https://github.com/OmniNode-ai/omnimarket", "OmniNode-ai/omnimarket"),
        ("https://github.com/OmniNode-ai/omnimarket/", "OmniNode-ai/omnimarket"),
        ("git@github.com:OmniNode-ai/omnimarket.git", "OmniNode-ai/omnimarket"),
        ("ssh://git@github.com/OmniNode-ai/omnimarket.git", "OmniNode-ai/omnimarket"),
        (
            "https://x-access-token@github.com/OmniNode-ai/omnimarket.git",
            "OmniNode-ai/omnimarket",
        ),
        ("https://gitlab.com/OmniNode-ai/omnimarket.git", None),
        ("/srv/mirrors/omnimarket.git", None),
        ("https://github.com/OmniNode-ai", None),
        ("", None),
    ],
)
def test_only_a_github_remote_names_a_repository(
    remote: str, expected: str | None
) -> None:
    assert github_repository_from_remote(remote) == expected


def test_a_bound_subject_without_a_commit_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError):
        ModelDodContractSubject(
            source=EnumDodContractSource.PRODUCT_REPOSITORY,
            repository="OmniNode-ai/omnimarket",
            commit_sha=None,
            repo_path=_CONTRACT_REL,
        )
    with pytest.raises(ValidationError):
        ModelDodContractSubject(
            source=EnumDodContractSource.ONEX_CHANGE_CONTROL,
            repository=None,
            commit_sha="a" * 40,
            repo_path=_CONTRACT_REL,
        )


def test_an_unbound_subject_cannot_claim_a_commit() -> None:
    with pytest.raises(ValidationError):
        ModelDodContractSubject(
            source=EnumDodContractSource.UNBOUND,
            repository="OmniNode-ai/omnimarket",
            commit_sha="a" * 40,
            repo_path=_CONTRACT_REL,
        )


def test_a_commit_must_be_a_full_object_id() -> None:
    with pytest.raises(ValidationError):
        ModelDodContractSubject(
            source=EnumDodContractSource.PRODUCT_REPOSITORY,
            repository="OmniNode-ai/omnimarket",
            commit_sha="abc1234",
            repo_path=_CONTRACT_REL,
        )


# ---------------------------------------------------------------------------
# The collector records the subject of the file it actually loaded
# ---------------------------------------------------------------------------


def test_the_collector_records_the_subject_of_the_contract_it_loaded(
    tmp_path: Path,
) -> None:
    repo = _checkout(tmp_path, "https://github.com/OmniNode-ai/omnimarket.git")
    collector = EvidenceCollector()
    collector._collect_impl(_TICKET, contract_path=str(repo / _CONTRACT_REL))

    subject = collector.contract_subject
    assert subject is not None
    assert subject.source is EnumDodContractSource.PRODUCT_REPOSITORY
    assert subject.commit_sha == _git(repo, "rev-parse", "HEAD")


def test_a_missing_contract_file_records_no_subject(tmp_path: Path) -> None:
    collector = EvidenceCollector()
    collector._collect_impl(_TICKET, contract_path=str(tmp_path / "absent.yaml"))
    assert collector.contract_subject is None


def test_an_inline_goal_contract_is_named_inline() -> None:
    collector = EvidenceCollector()
    collector._collect_impl(_TICKET, inline_items=())
    subject = collector.contract_subject
    assert subject is not None
    assert subject.source is EnumDodContractSource.INLINE_GOAL
    assert subject.repository is None
    assert subject.commit_sha is None


# ---------------------------------------------------------------------------
# The verify handler carries it onto the published state and the event twin
# ---------------------------------------------------------------------------


_BOUND = ModelDodContractSubject(
    source=EnumDodContractSource.PRODUCT_REPOSITORY,
    repository="OmniNode-ai/omnimarket",
    commit_sha="0123456789abcdef0123456789abcdef01234567",
    repo_path=_CONTRACT_REL,
)


class _StubCollector:
    occ_governance_ref = None
    occ_refresh_outcome = None
    occ_resolved_sha = None
    occ_ref_failure_code = None
    occ_ref_failure_cause = None
    lookup_failure_cause = None
    lookup_failure_code = None
    acceptance_summary = None

    def __init__(self, subject: ModelDodContractSubject | None) -> None:
        if subject is not None:
            self.contract_subject = subject

    def collect(self, **_: object) -> list[ModelEvidenceCheckResult]:
        return [
            ModelEvidenceCheckResult(
                evidence_id="dod-001",
                description="check dod-001",
                status=EnumEvidenceCheckStatus.VERIFIED,
            )
        ]


def _verify(
    monkeypatch: pytest.MonkeyPatch, subject: ModelDodContractSubject | None
) -> tuple[ModelDodVerifyState, Any]:
    handler = HandlerDodVerify()
    monkeypatch.setattr(handler, "_make_collector", lambda: _StubCollector(subject))
    return handler.run_verification(
        ModelDodVerifyStartCommand(
            ticket_id=_TICKET,
            correlation_id=uuid4(),
            contract_path="/checkout/contracts/OMN-20696.yaml",
            execution_audience="hosted",
        )
    )


def test_the_published_state_carries_the_contract_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, completed = _verify(monkeypatch, _BOUND)
    payload = state.model_dump(mode="json")

    assert payload["contract_source"] == "product_repository"
    assert payload["contract_repository"] == "OmniNode-ai/omnimarket"
    assert payload["contract_commit_sha"] == _BOUND.commit_sha
    assert payload["contract_repo_path"] == _CONTRACT_REL
    assert completed.contract_source is EnumDodContractSource.PRODUCT_REPOSITORY
    assert completed.contract_commit_sha == _BOUND.commit_sha


def test_a_collector_that_records_no_subject_publishes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stub predating the attribute is "not measured", never a fabricated one."""
    state, completed = _verify(monkeypatch, None)
    payload = state.model_dump(mode="json")
    for field in _SUBJECT_FIELDS:
        assert field not in payload
        assert field not in completed.model_dump(mode="json")


def test_caller_supplied_evidence_carries_no_subject() -> None:
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(ticket_id=_TICKET, correlation_id=uuid4()),
        evidence_results=[
            ModelEvidenceCheckResult(
                evidence_id="dod-001",
                description="check dod-001",
                status=EnumEvidenceCheckStatus.VERIFIED,
            )
        ],
    )
    assert isinstance(state, ModelDodVerifyState)
    assert state.contract_source is None
    assert "contract_source" not in state.model_dump(mode="json")


# ---------------------------------------------------------------------------
# The projection stores it
# ---------------------------------------------------------------------------


_STARTED = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "correlation_id": str(uuid4()),
        "ticket_id": _TICKET,
        "status": "verified",
        "started_at": _STARTED.isoformat(),
        "completed_at": (_STARTED + timedelta(minutes=2)).isoformat(),
        "total_checks": 3,
        "verified_count": 3,
        "behavior_proving_count": 1,
        "contract_source": "product_repository",
        "contract_repository": "OmniNode-ai/omnimarket",
        "contract_commit_sha": _BOUND.commit_sha,
        "contract_repo_path": _CONTRACT_REL,
    }
    base.update(overrides)
    return base


def _row(payload: dict[str, Any]) -> ModelDodVerdictRow | None:
    wire = ModelDodVerdictWire.model_validate(payload)
    return (
        HandlerProjectionDodVerdict()
        .handle(ModelDodVerdictProjectionRequest(event=wire))
        .row
    )


def test_the_row_stores_the_contract_subject() -> None:
    row = _row(_event())
    assert row is not None
    assert row.contract_source is EnumDodContractSource.PRODUCT_REPOSITORY
    assert row.contract_repository == "OmniNode-ai/omnimarket"
    assert row.contract_commit_sha == _BOUND.commit_sha
    assert row.contract_repo_path == _CONTRACT_REL


def test_an_older_event_with_no_subject_projects_nulls() -> None:
    payload = _event()
    for field in _SUBJECT_FIELDS:
        payload.pop(field)
    row = _row(payload)
    assert row is not None
    for field in _SUBJECT_FIELDS:
        assert getattr(row, field) is None


def test_the_end_to_end_published_state_reaches_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = _verify(monkeypatch, _BOUND)
    row = _row(state.model_dump(mode="json"))
    assert row is not None
    assert row.contract_source is EnumDodContractSource.PRODUCT_REPOSITORY
    assert row.contract_commit_sha == _BOUND.commit_sha


class _RecordingDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return [
            {
                "ticket_id": args[0],
                "correlation_id": args[1],
                "completed_at": args[2],
                "outcome": args[15],
                "outcome_refusal": args[16],
            }
        ]


def test_the_writer_binds_the_subject_into_its_named_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = DodVerdictProjectionWriter()
    db = _RecordingDb()
    monkeypatch.setattr(writer, "_db", db)
    report = writer.handle(_event())
    assert report["rows_upserted"] == 1

    sql, args = db.calls[0]
    insert_columns = [
        column.strip() for column in sql.split("(", 1)[1].split(")", 1)[0].split(",")
    ]
    bound = dict(zip(insert_columns, args, strict=True))
    assert bound["contract_source"] == "product_repository"
    assert bound["contract_repository"] == "OmniNode-ai/omnimarket"
    assert bound["contract_commit_sha"] == _BOUND.commit_sha
    assert bound["contract_repo_path"] == _CONTRACT_REL
    for field in _SUBJECT_FIELDS:
        assert f"{field} = EXCLUDED.{field}" in sql


def test_the_migration_adds_the_subject_columns_additively() -> None:
    migration = _PROJECTION / "migrations/0004_dod_verify_runs_contract_subject.sql"
    ddl = migration.read_text()
    for field in _SUBJECT_FIELDS:
        assert f"ADD COLUMN IF NOT EXISTS {field}" in ddl
    assert "DROP " not in ddl.upper().replace("DROP CONSTRAINT IF EXISTS", "")
    assert writer_module.TABLE == "omninode_internal.dod_verify_runs"


# ---------------------------------------------------------------------------
# Rebuildable from events: the fold is a function of the event alone
# ---------------------------------------------------------------------------


def _dedupe_key() -> tuple[str, ...]:
    contract = yaml.safe_load((_PROJECTION / "contract.yaml").read_text())
    return tuple(contract["db_io"]["dedupe_key"])


def _project(stream: list[dict[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
    """Fold a stream into the table the upsert would leave: last write per key."""
    key = _dedupe_key()
    table: dict[tuple[Any, ...], dict[str, Any]] = {}
    for payload in stream:
        row = _row(payload)
        if row is None:
            continue
        dumped = row.model_dump(mode="json")
        table[tuple(dumped[column] for column in key)] = dumped
    return table


def _history() -> list[dict[str, Any]]:
    """Three attempts on one ticket, one on another, a rehearsal, an OCC run."""
    first = _event(status="failed", failed_count=1, verified_count=2)
    second = _event(
        completed_at=(_STARTED + timedelta(minutes=9)).isoformat(),
        contract_commit_sha="f" * 40,
    )
    shared = uuid4()
    third = _event(
        correlation_id=str(shared),
        completed_at=(_STARTED + timedelta(minutes=20)).isoformat(),
    )
    # The same correlation id re-verified later is a new run, not an overwrite.
    third_again = _event(
        correlation_id=str(shared),
        completed_at=(_STARTED + timedelta(minutes=30)).isoformat(),
    )
    other = _event(
        ticket_id="OMN-20070",
        contract_source="onex_change_control",
        contract_repository="OmniNode-ai/onex_change_control",
        contract_commit_sha="e" * 40,
        contract_repo_path="contracts/OMN-20070.yaml",
    )
    rehearsal = _event(dry_run=True)
    return [first, second, third, third_again, other, rehearsal]


def test_the_key_the_rebuild_relies_on_is_the_declared_dedupe_key() -> None:
    assert _dedupe_key() == ("ticket_id", "correlation_id", "completed_at")


def test_a_replay_with_redeliveries_in_any_order_rebuilds_the_same_table() -> None:
    history = _history()
    original = _project(history)
    assert len(original) == 5  # the rehearsal is not an attempt

    rng = random.Random(20696)
    for _ in range(25):
        replay = history + rng.sample(history, k=3)
        rng.shuffle(replay)
        assert _project(replay) == original


def test_a_rebuilt_table_keeps_the_contract_subject_of_every_run() -> None:
    table = _project(_history())
    sources = sorted(row["contract_source"] for row in table.values())
    assert sources == [
        "onex_change_control",
        "product_repository",
        "product_repository",
        "product_repository",
        "product_repository",
    ]
    by_commit = {
        row["contract_commit_sha"]
        for row in table.values()
        if row["ticket_id"] == _TICKET
    }
    assert by_commit == {_BOUND.commit_sha, "f" * 40}


def test_the_fold_reads_no_clock_so_a_rebuild_reproduces_every_field() -> None:
    payload = _event()
    first = _row(payload)
    second = _row(dict(payload))
    assert first is not None
    assert second is not None
    assert first == second
    assert isinstance(UUID(str(first.correlation_id)), UUID)
