# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation_events row carries the terminal's complete cohort key (OMN-18930).

K3 of OMN-18925. The plan's K3 row requires that "each compared run's row
carries its complete key, and the two rows differ only where the keys differ".
The key itself is typed in omnibase_infra (``ModelDelegationCohortKey``,
omnibase_infra#4054); this module proves the projection half:

* the pure fold reads the key off the terminal and returns the three row
  columns, never a dimension the terminal did not carry;
* an incomplete key (the offset-489 historical record, missing exactly five
  dimensions) is refused by name and no key is stored;
* the digest the fold stores equals ``ModelDelegationCohortKey.key_sha256`` for
  the real dev-lane captures A and B (pinned below from the infra model);
* the writer names the three columns when a key was carried, and names none of
  them when it was not, so a later keyless re-emit cannot clobber a stored key.

The fixtures are the real 2026-09-24 .201 dev-lane captures from omnibase_infra
tests/fixtures/delegation/omn18930_lab (terminal payloads) and the keys the
infra assembler built from them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_cohort_key_fold import (
    DELEGATION_COHORT_KEY_DIMENSIONS,
    HandlerDelegationCohortKeyFold,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)

pytestmark = pytest.mark.unit

_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "delegation"
    / "omn18930_cohort_key"
)

#: ``ModelDelegationCohortKey.key_sha256`` of the two captured keys, computed by
#: the omnibase_infra model at head cc4020e33 (omnibase_infra#4054). Capture A2
#: (same build as A) produced the same digest as A.
_KEY_A_SHA256 = "70236fddc6268401bdbae9d3093c15faab7f2371175dcbacd3bd49413ab1a73b"
_KEY_B_SHA256 = "3f8d5b956e1e225e47a2481e8cd5d893e6f6bc361748ef4817fdd45fb01d120c"

_COHORT_COLUMNS = ("cohort_key", "cohort_key_sha256", "cohort_key_refusal")


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _terminal(capture: str, key: object = None) -> ModelDelegateSkillTerminalProjection:
    payload = _load(f"terminal_payload_{capture}.json")
    if key is not None:
        payload["cohort_key"] = key
    return ModelDelegateSkillTerminalProjection.from_payload(payload)


def _fold(terminal: ModelDelegateSkillTerminalProjection) -> Any:
    return HandlerDelegationCohortKeyFold().handle(terminal)


def test_dimension_list_is_the_ten_named_dimensions() -> None:
    """The fold checks exactly the dimensions the infra model declares."""
    assert DELEGATION_COHORT_KEY_DIMENSIONS == (
        "prompt_sha256",
        "resolved_task_type",
        "response_contract_sha256",
        "lane",
        "build_identity",
        "consumer_identity",
        "first_hop_identity",
        "provider_policy",
        "deadline_seconds",
        "retry_bounds",
    )
    assert set(_load("cohort_key_A.json")) == set(DELEGATION_COHORT_KEY_DIMENSIONS)


def test_complete_key_is_carried_with_the_infra_digest() -> None:
    key_a = _load("cohort_key_A.json")
    folded = _fold(_terminal("A", key_a))
    assert folded.cohort_key == key_a
    assert folded.cohort_key_sha256 == _KEY_A_SHA256
    assert folded.cohort_key_refusal is None


def test_keys_from_different_builds_differ_only_in_build_identity() -> None:
    key_a = _load("cohort_key_A.json")
    key_b = _load("cohort_key_B.json")
    folded_a = _fold(_terminal("A", key_a))
    folded_b = _fold(_terminal("B", key_b))
    assert folded_b.cohort_key_sha256 == _KEY_B_SHA256
    assert folded_a.cohort_key_sha256 != folded_b.cohort_key_sha256
    changed = [
        name
        for name in DELEGATION_COHORT_KEY_DIMENSIONS
        if folded_a.cohort_key[name] != folded_b.cohort_key[name]
    ]
    assert changed == ["build_identity"]


def test_digest_ignores_member_order() -> None:
    key_a = _load("cohort_key_A.json")
    reordered = dict(reversed(list(key_a.items())))
    assert _fold(_terminal("A", reordered)).cohort_key_sha256 == _KEY_A_SHA256


def test_absent_key_writes_no_cohort_column() -> None:
    folded = _fold(_terminal("A"))
    assert folded.cohort_key is None
    assert folded.cohort_key_sha256 is None
    assert folded.cohort_key_refusal is None
    assert folded.row_columns() == {}


def test_offset489_incomplete_key_is_refused_on_its_five_unproven_dimensions() -> None:
    folded = _fold(_terminal("A", _load("offset489_incomplete_cohort_key.json")))
    assert folded.cohort_key is None
    assert folded.cohort_key_sha256 is None
    assert folded.cohort_key_refusal == (
        "missing dimensions: build_identity, consumer_identity, provider_policy, "
        "deadline_seconds, retry_bounds"
    )
    assert folded.row_columns() == {
        "cohort_key": None,
        "cohort_key_sha256": None,
        "cohort_key_refusal": folded.cohort_key_refusal,
    }


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda key: key.update(extra="x"), "unknown dimensions: extra"),
        (
            lambda key: key.update(build_identity=None),
            "build_identity must be an object",
        ),
        (
            lambda key: key["build_identity"].update(source_revision="0ddd10ca"),
            "build_identity.source_revision must be a full lowercase git sha",
        ),
        (
            lambda key: key["build_identity"].update(image_digest="latest"),
            "build_identity.image_digest must be sha256:<64 lowercase hex>",
        ),
        (
            lambda key: key["build_identity"].pop("build_provenance_sha256"),
            "build_identity.build_provenance_sha256 must be lowercase SHA-256 hex",
        ),
        (
            lambda key: key.update(prompt_sha256="abc"),
            "prompt_sha256 must be lowercase SHA-256 hex",
        ),
        (
            lambda key: key.update(response_contract_sha256="abc"),
            "response_contract_sha256 must be lowercase SHA-256 hex or null",
        ),
        (lambda key: key.update(lane=" "), "lane must be a nonblank trimmed string"),
        (
            lambda key: key.update(deadline_seconds=True),
            "deadline_seconds must be a positive number",
        ),
        (
            lambda key: key.update(deadline_seconds=0),
            "deadline_seconds must be a positive number",
        ),
        (
            lambda key: key.update(retry_bounds={}),
            "retry_bounds must be a nonempty object",
        ),
    ],
)
def test_malformed_key_is_refused_by_name(mutate: Any, expected: str) -> None:
    key = _load("cohort_key_A.json")
    mutate(key)
    folded = _fold(_terminal("A", key))
    assert folded.cohort_key is None
    assert folded.cohort_key_sha256 is None
    assert folded.cohort_key_refusal == expected


def test_null_response_contract_is_an_explicit_absence_not_a_refusal() -> None:
    key = _load("cohort_key_A.json")
    key["response_contract_sha256"] = None
    folded = _fold(_terminal("A", key))
    assert folded.cohort_key_refusal is None
    assert folded.cohort_key is not None
    assert folded.cohort_key["response_contract_sha256"] is None


def test_non_object_key_is_refused_without_losing_the_terminal() -> None:
    """A malformed key must not dead-letter the delegation's own row."""
    terminal = _terminal("A", "not-a-key")
    folded = _fold(terminal)
    assert folded.cohort_key_refusal == "cohort_key must be a JSON object"


class _RecordingDb:
    """Stands in for the sync adapter; records the row the writer upserts."""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def query(
        self, table: str, filters: dict[str, object], **_: object
    ) -> list[dict[str, Any]]:
        return []

    def upsert(self, table: str, conflict_key: object, row: dict[str, object]) -> bool:
        self.rows.append(dict(row))
        return True

    def upsert_returning(
        self,
        table: str,
        conflict_key: object,
        row: dict[str, object],
        **_: object,
    ) -> list[dict[str, object]]:
        self.rows.append(dict(row))
        # The database stamps the write attestation; the writer requires it.
        return [{**row, "written_at": datetime.now(tz=UTC)}]


def _written_row(terminal: ModelDelegateSkillTerminalProjection) -> dict[str, object]:
    db = _RecordingDb()
    HandlerProjectionDelegation(
        publisher=_NullPublisher()
    ).project_delegate_skill_terminal(
        terminal,
        db,  # type: ignore[arg-type]
    )
    assert len(db.rows) == 1
    return db.rows[0]


class _NullPublisher:
    def publish(self, *args: object, **kwargs: object) -> None:
        return None


def test_sync_writer_names_the_key_columns_when_the_terminal_carries_a_key() -> None:
    row = _written_row(_terminal("A", _load("cohort_key_A.json")))
    assert row["cohort_key"] == _load("cohort_key_A.json")
    assert row["cohort_key_sha256"] == _KEY_A_SHA256
    assert row["cohort_key_refusal"] is None


def test_sync_writer_names_no_key_column_for_a_keyless_terminal() -> None:
    row = _written_row(_terminal("A"))
    assert not set(_COHORT_COLUMNS) & set(row)
