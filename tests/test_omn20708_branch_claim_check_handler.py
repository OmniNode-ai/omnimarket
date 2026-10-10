# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Planted evidence through the real contract-loaded handler."""

import ast
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.model_github_pr_state_observation import (
    ModelGitHubPrStateObservation,
)
from omnimarket.nodes.node_branch_claim_check_effect.handlers.handler_branch_claim_check import (
    HandlerBranchClaimCheck,
)
from omnimarket.nodes.node_branch_claim_check_effect.models import (
    ModelBranchClaimCheckRequest,
)
from tests.test_omn20708_branch_claim_resolution import (
    CONTRACT,
    NOW,
    POLICY,
    commit,
    db_rows,
    row,
)

pytestmark = pytest.mark.unit


@dataclass
class Reader:
    rows: tuple = ()
    error: Exception | None = None
    calls: list = field(default_factory=list)

    def read_rows(self, *, ticket, since, until):
        self.calls.append((ticket, since, until))
        if self.error:
            raise self.error
        return tuple(r for r in self.rows if since <= r[1] <= until and ticket in r[2])

    def read_row_ids(self, *, since, until):
        self.calls.append("ids")
        return frozenset(r[0] for r in self.rows if since <= r[1] <= until)

    def newest_row_ts(self, *, until):
        self.calls.append("newest")
        return max((r[1] for r in self.rows if r[1] <= until), default=None)


@dataclass
class Witness:
    rows: list = field(default_factory=list)
    error: Exception | None = None
    calls: int = 0

    def read_rows(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.rows


@dataclass
class Commits:
    rows: list = field(default_factory=lambda: [commit()])
    error: Exception | None = None
    calls: int = 0

    def list_commits(self, repo, pr_number):
        self.calls += 1
        if self.error:
            raise self.error
        return self.rows


@dataclass
class Poster:
    posts: list = field(default_factory=list)
    error: Exception | None = None

    def post(self, **kwargs):
        self.posts.append(kwargs)
        if self.error:
            raise self.error


def request(**updates):
    values = {
        "correlation_id": uuid4(),
        "repo": POLICY.repositories[0],
        "pr_number": 1,
        "head_sha": "a" * 40,
        "head_ref": "lane/omn-1-change",
    }
    values.update(updates)
    return ModelBranchClaimCheckRequest(**values)


def observation(**updates):
    values = {
        "entity_id": f"{POLICY.repositories[0]}#1",
        "repo": POLICY.repositories[0],
        "pr_number": 1,
        "delivery_id": str(uuid4()),
        "github_event": "pull_request",
        "as_of": NOW.isoformat(),
        "ci_status": "PENDING",
        "head_sha": "a" * 40,
        "head_ref": "lane/omn-1-change",
    }
    values.update(updates)
    return ModelGitHubPrStateObservation.model_validate(values)


def handler(reader=None, witness=None, commits=None, poster=None, contract=CONTRACT):
    return HandlerBranchClaimCheck(
        contract_path=contract,
        now=lambda: NOW,
        reader=reader if reader is not None else Reader(),
        witness=witness if witness is not None else Witness(),
        commit_reader=commits if commits is not None else Commits(),
        poster=poster if poster is not None else Poster(),
    )


@pytest.mark.parametrize("db_only", [False, True])
def test_database_claim_decides_holder(db_only):
    claim = row()
    witness = [row("STATUS", hours=0)] if db_only else [claim]
    result = handler(Reader(db_rows([claim])), Witness(witness)).handle(request())
    assert result.outcome.value == "held-by-pusher"
    assert result.holder_lane == "lane-a"
    assert result.holder_row == "work_ledger_rows:" + db_rows([claim])[0][0][:12]
    assert result.rows_read == 1
    assert result.check_posted


@pytest.mark.parametrize(
    ("lane", "ticket", "outcome", "conclusion"),
    [
        ("lane-b", "OMN-1", "held-elsewhere", "neutral"),
        ("lane-a", "OMN-1", "held-by-pusher", "success"),
        ("lane-b", "OMN-2", "unclaimed", "success"),
    ],
)
def test_planted_pair(lane, ticket, outcome, conclusion):
    claim = row()
    poster = Poster()
    result = handler(
        Reader(db_rows([claim])), Witness([claim]), Commits([commit(lane)]), poster
    ).handle(request(head_ref=ticket))
    assert (result.outcome.value, result.conclusion) == (outcome, conclusion)
    posted = poster.posts[0]
    assert posted["conclusion"] == conclusion
    assert posted["name"] == POLICY.check_name
    holder = "lane-a" if ticket == "OMN-1" else "-"
    assert f"holder={holder}" in posted["summary"].splitlines()[0]
    if holder != "-":
        assert holder in posted["title"]
    assert (
        posted["summary"].splitlines()[0]
        == f"branch-claim-outcome: {outcome} ticket={ticket} holder={holder} window=2026-09-11T12:00:00Z..2026-09-13T12:00:00Z rows={result.rows_read}"
    )


@pytest.mark.parametrize("kind", ["CLAIM", "TERMINAL"])
def test_missing_transition_fails_closed(kind):
    witness = [
        row(
            kind,
            extra="outcome=done | friction=none" if kind == "TERMINAL" else "taking it",
        )
    ]
    result = handler(witness=Witness(witness)).handle(request())
    assert result.outcome.value == "did-not-run"
    assert result.conclusion == "failure"
    assert "missing from work_ledger_rows" in result.cause
    assert "check did not decide" in result.cause
    assert result.findings == tuple(witness)
    assert result.window_missing_claim_rows == 1


def test_other_ticket_gap_is_visibility_only():
    result = handler(witness=Witness([row(ticket="OMN-2")])).handle(request())
    assert result.outcome.value == "unclaimed"
    assert result.window_missing_claim_rows == 1


@pytest.mark.parametrize(
    ("reader", "witness", "commits", "cause"),
    [
        (
            Reader(error=OSError("offline")),
            Witness(),
            Commits(),
            "work ledger database unreadable",
        ),
        (
            Reader(),
            Witness(error=OSError("absent")),
            Commits(),
            "parity witness unreadable",
        ),
        (
            Reader(db_rows([row(hours=0)])),
            Witness([row(hours=1)]),
            Commits(),
            "parity witness is behind the database",
        ),
        (
            Reader(),
            Witness(),
            Commits(error=OSError("network")),
            "pull request commits unreadable",
        ),
        (
            Reader(),
            Witness(),
            Commits([commit()] * POLICY.max_pr_commits),
            "commit list may be truncated",
        ),
    ],
)
def test_unreadable_inputs_fail_closed(reader, witness, commits, cause):
    poster = Poster()
    result = handler(reader, witness, commits, poster).handle(request())
    assert result.outcome.value == "did-not-run"
    assert result.conclusion == "failure"
    assert cause in result.cause
    assert "check did not decide" in result.cause
    assert len(poster.posts) == 1
    assert poster.posts[0]["conclusion"] == "failure"


@pytest.mark.parametrize(
    "updates",
    [
        {"github_event": "check_run"},
        {"ci_status": "FAILURE"},
        {"ci_status": None, "triage_state": "CLOSED"},
        {"repo": "other/repo"},
        {"head_sha": None},
        {"head_ref": None},
    ],
)
def test_observation_filtering_has_no_io(updates):
    reader, witness, commits, poster = Reader(), Witness(), Commits(), Poster()
    result = handler(reader, witness, commits, poster).handle(observation(**updates))
    assert result.outcome.value == "not-applicable"
    assert not reader.calls
    assert witness.calls == commits.calls == 0
    assert not poster.posts


def test_other_repo_command_has_no_io():
    reader, witness, commits, poster = Reader(), Witness(), Commits(), Poster()
    assert (
        handler(reader, witness, commits, poster)
        .handle(request(repo="other/repo"))
        .outcome.value
        == "not-applicable"
    )
    assert not reader.calls
    assert not witness.calls
    assert not commits.calls
    assert not poster.posts


def test_no_ticket_posts_success_without_reading_evidence():
    reader, witness, commits, poster = Reader(), Witness(), Commits(), Poster()
    result = handler(reader, witness, commits, poster).handle(request(head_ref="main"))
    assert result.outcome.value == "no-ticket"
    assert result.check_posted
    assert not reader.calls
    assert not witness.calls
    assert not commits.calls


def test_poster_failure_recorded_and_logged(caplog):
    result = handler(poster=Poster(error=OSError("post failed"))).handle(request())
    assert not result.check_posted
    assert result.post_error == "OSError: post failed"
    assert "post failed" in caplog.text


def test_thresholds_loaded_from_modified_contract(tmp_path):
    raw = yaml.safe_load(CONTRACT.read_text())
    raw["branch_claim"].update(
        read_window_hours=24, staleness_hours=1, check_name="modified check"
    )
    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(raw))
    reader, poster = Reader(db_rows([row()])), Poster()
    result = handler(
        reader, Witness([row()]), Commits([commit("lane-b")]), poster, contract
    ).handle(request())
    assert result.outcome.value == "unclaimed"
    assert result.window_since == NOW - timedelta(hours=24)
    assert poster.posts[0]["name"] == "modified check"


def test_thresholds_not_literals_and_handler_uses_only_boundaries():
    for path in CONTRACT.parent.rglob("*.py"):
        tree = ast.parse(path.read_text())
        assert not [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, int)
            and n.value in {48, 12, 250}
        ]
    path = CONTRACT.parent / "handlers/handler_branch_claim_check.py"
    tree = ast.parse(path.read_text())
    assert not [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "open")
            or (
                isinstance(n.func, ast.Attribute)
                and n.func.attr in {"read_text", "read_bytes", "open"}
            )
        )
    ]
    assert "import psycopg2" not in path.read_text()
    assert "import asyncpg" not in path.read_text()


def test_policy_fails_closed(tmp_path):
    path = tmp_path / "contract.yaml"
    path.write_text("branch_claim: {}")
    with pytest.raises(ValueError, match="branch claim contract unreadable"):
        handler(contract=path)


def test_real_defaults_filter_without_credentials_or_database(monkeypatch):
    for name in (
        POLICY.ledger_source.dsn_env,
        POLICY.parity_witness.ledger_path_env,
        "ONEXBOT_OCC_APP_ID",
        "ONEXBOT_OCC_PRIVATE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    node = HandlerBranchClaimCheck(now=lambda: NOW)
    assert (
        node.handle(observation(github_event="check_run")).outcome.value
        == "not-applicable"
    )


def test_file_witness_reads_bounded_archives_oldest_first(tmp_path, monkeypatch):
    from omnimarket.nodes.node_branch_claim_check_effect.handlers.ledger_witness_reader import (
        FileLedgerWitness,
    )

    live = tmp_path / "ROLLING_WORK_LEDGER.md"
    archive = tmp_path / POLICY.parity_witness.archive_dir_name
    archive.mkdir()
    old, first, second, current = (
        row(hours=80),
        row(hours=40),
        row(hours=20),
        row(hours=1),
    )
    live.write_text(current + "\n")
    (archive / "ROLLING_WORK_LEDGER_2026-09-10-split.md").write_text(old)
    (archive / "ROLLING_WORK_LEDGER_2026-09-12-split.md").write_text(second)
    (archive / "ROLLING_WORK_LEDGER_2026-09-11-split.md").write_text(
        first + "\n  continued scope\n"
    )
    monkeypatch.setenv(POLICY.parity_witness.ledger_path_env, str(live))
    reader = FileLedgerWitness(
        POLICY.parity_witness, since=NOW - timedelta(hours=POLICY.read_window_hours)
    )
    assert reader.read_rows() == [first + "\n  continued scope", second, current]


def test_file_witness_absent_fails_closed(tmp_path, monkeypatch):
    from omnimarket.nodes.node_branch_claim_check_effect.handlers.ledger_witness_reader import (
        FileLedgerWitness,
    )

    monkeypatch.setenv(
        POLICY.parity_witness.ledger_path_env, str(tmp_path / "missing.md")
    )
    result = handler(
        witness=FileLedgerWitness(POLICY.parity_witness, since=NOW)
    ).handle(request())
    assert result.outcome.value == "did-not-run"
    assert "parity witness unreadable" in result.cause


def test_a_transition_the_database_cannot_carry_fails_closed():
    # HANDOVER is not an emitted row type, so the database replay can never see
    # it; an answer computed without it would name the wrong holder.
    result = handler(witness=Witness([row("HANDOVER", extra="to=lane-b")])).handle(
        request()
    )
    assert result.outcome.value == "did-not-run"
    assert "missing from work_ledger_rows" in result.cause
    assert result.window_missing_claim_rows == 1


def test_real_commit_reader_paginates_with_app_auth(monkeypatch):
    import omnimarket.nodes.node_branch_claim_check_effect.handlers.pr_commit_reader as module

    tokens, pages = [], []

    def token(path, **kwargs):
        tokens.append((path, kwargs))
        return "app-token"

    def get(method, path, *, token):
        pages.append((method, path, token))
        count = POLICY.commits_per_page if path.endswith("&page=1") else 1
        return [
            {"sha": "a" * 40, "commit": {"message": commit()[1]}} for _ in range(count)
        ]

    monkeypatch.setattr(module, "resolve_app_installation_token_from_contract", token)
    monkeypatch.setattr(module, "rest_json_array", get)
    reader = module.GitHubPrCommitReader(
        CONTRACT, per_page=POLICY.commits_per_page, max_commits=POLICY.max_pr_commits
    )
    assert (
        len(reader.list_commits(POLICY.repositories[0], 7))
        == POLICY.commits_per_page + 1
    )
    assert [p[1] for p in pages] == [
        f"/repos/OmniNode-ai/omniclaude/pulls/7/commits?per_page=100&page={page}"
        for page in (1, 2)
    ]
    assert tokens == [
        (CONTRACT, {"org": "OmniNode-ai", "repositories": ["omniclaude"]})
    ]


def test_real_poster_uses_check_run_app_mode(monkeypatch):
    import omnimarket.nodes.node_branch_claim_check_effect.handlers.check_run_poster as module

    calls = []
    monkeypatch.setattr(
        module,
        "resolve_app_installation_token_from_contract",
        lambda _path, **_kwargs: "app-token",
    )
    monkeypatch.setattr(
        module, "rest_json", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    module.GitHubCheckRunPoster(CONTRACT).post(
        repo=POLICY.repositories[0],
        head_sha="b" * 40,
        name=POLICY.check_name,
        conclusion="neutral",
        title="held-elsewhere: OMN-1 lane-a",
        summary="marker",
    )
    args, kwargs = calls[0]
    assert args == ("POST", "/repos/OmniNode-ai/omniclaude/check-runs")
    assert kwargs["token"] == "app-token"
    assert kwargs["body"]["head_sha"] == "b" * 40
    assert kwargs["body"]["status"] == "completed"
    assert kwargs["body"]["conclusion"] == "neutral"


def test_database_reader_is_lazy_parameterized_and_closes_on_error(monkeypatch):
    from unittest.mock import MagicMock

    from omnimarket.nodes.node_branch_claim_check_effect.handlers.work_ledger_row_reader import (
        PostgresWorkLedgerRowReader,
    )
    from omnimarket.projection import postgres_read_database

    conn = MagicMock()
    conn.closed = False
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = list(db_rows([row()]))
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(postgres_read_database, "connect_read_only", connect)
    monkeypatch.setenv(
        POLICY.ledger_source.dsn_env, "postgresql://user:secret@example/db"
    )
    reader = PostgresWorkLedgerRowReader(POLICY.ledger_source)
    connect.assert_not_called()
    reader.read_rows(ticket="OMN-1%_", since=NOW, until=NOW)
    sql, params = cursor.execute.call_args.args
    assert (
        sql
        == "SELECT row_id, row_ts, raw_row FROM omninode_internal.work_ledger_rows WHERE row_ts >= %s AND row_ts <= %s AND raw_row LIKE %s ORDER BY row_ts, row_id"
    )
    assert params == (NOW, NOW, "%OMN-1\\%\\_%")
    cursor.execute.side_effect = OSError(
        "postgresql://user:secret@example/db unavailable"
    )
    with pytest.raises(RuntimeError, match="redacted") as error:
        reader.read_rows(ticket="OMN-1", since=NOW, until=NOW)
    assert "secret" not in str(error.value)
    conn.close.assert_called_once()


def test_database_default_unset_dsn_becomes_failure(monkeypatch):
    from omnimarket.nodes.node_branch_claim_check_effect.handlers.work_ledger_row_reader import (
        PostgresWorkLedgerRowReader,
    )

    monkeypatch.delenv(POLICY.ledger_source.dsn_env, raising=False)
    result = handler(reader=PostgresWorkLedgerRowReader(POLICY.ledger_source)).handle(
        request()
    )
    assert result.outcome.value == "did-not-run"
    assert "work ledger database unreadable" in result.cause
    assert POLICY.ledger_source.dsn_env in result.cause


def test_parity_window_excludes_old_and_future_transitions():
    result = handler(witness=Witness([row(hours=49), row(hours=-1)])).handle(request())
    assert result.outcome.value == "unclaimed"
    assert result.window_missing_claim_rows == 0


@pytest.mark.parametrize("method", ["read_row_ids", "newest_row_ts"])
def test_secondary_database_reads_fail_closed(method, monkeypatch):
    reader = Reader()

    def unreadable(**_kwargs):
        raise OSError("database read failed")

    monkeypatch.setattr(reader, method, unreadable)
    result = handler(reader=reader).handle(request())
    assert result.outcome.value == "did-not-run"
    assert "work ledger database unreadable" in result.cause


def test_replay_errors_fail_closed(monkeypatch):
    import omnimarket.nodes.node_branch_claim_check_effect.handlers.handler_branch_claim_check as module

    def broken(*_args, **_kwargs):
        raise ValueError("bad row")

    monkeypatch.setattr(module, "build_index", broken)
    result = handler().handle(request())
    assert result.outcome.value == "did-not-run"
    assert "work ledger claim resolution unreadable" in result.cause


def test_contract_evidence_references_real_tests():
    import importlib

    contract = yaml.safe_load(CONTRACT.read_text())
    root = CONTRACT.parents[4]
    for entry in contract["dod_evidence"]:
        file_name, test_name = entry["test"].split("::")
        assert (root / file_name).is_file()
        module = importlib.import_module(
            file_name.removesuffix(".py").replace("/", ".")
        )
        assert callable(getattr(module, test_name))
    assert contract["externally_consumed_topics"] == [contract["terminal_event"]]


def test_terminal_result_frozen_and_forbids_extra_fields():
    from pydantic import ValidationError

    from omnimarket.nodes.node_branch_claim_check_effect.models import (
        ModelBranchClaimCheckResult,
    )

    result = handler().handle(request())
    with pytest.raises(ValidationError, match="frozen"):
        result.rows_read = 2
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelBranchClaimCheckResult.model_validate(
            {**result.model_dump(), "unexpected": True}
        )


def test_database_exception_never_exposes_dsn_in_check(monkeypatch):
    dsn = "postgresql://secret-user:secret-password@private/database"
    monkeypatch.setenv(POLICY.ledger_source.dsn_env, dsn)
    poster = Poster()
    result = handler(
        reader=Reader(error=OSError(f"cannot read {dsn}")), poster=poster
    ).handle(request())
    assert result.outcome.value == "did-not-run"
    assert dsn not in result.cause
    assert "secret-password" not in poster.posts[0]["summary"]
