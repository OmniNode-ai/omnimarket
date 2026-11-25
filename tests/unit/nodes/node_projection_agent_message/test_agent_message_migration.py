# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent message migration is reviewed here: what it creates, that it can run twice, and that it destroys nothing."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_agent_message.handlers.handler_agent_message_tables import (
    AGENT_MESSAGE_COLUMNS,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_projection_agent_message"
)
CREATE = NODE / "migrations/0000_create_agent_messages.sql"
GRANT = NODE / "migrations/0001_grant_tenant_projection_writer_agent_messages.sql"
RESERVED = {"from", "to", "offset", "partition", "order", "user", "group"}


def _statements(path: Path) -> list[str]:
    text = re.sub(r"--[^\n]*", "", path.read_text())
    return [s.strip() for s in text.split(";") if s.strip()]


def _create_table_body() -> str:
    text = re.sub(r"--[^\n]*", "", CREATE.read_text())
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS public\.agent_messages \((.*?)\n\);", text, re.S
    )
    assert match, "the migration must create public.agent_messages"
    return match.group(1)


def test_the_migrations_are_where_the_contract_says() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    tables = contract["db_io"]["db_tables"]
    assert [t["name"] for t in tables] == ["agent_messages"]
    assert tables[0]["migration"] == CREATE.name
    assert CREATE.is_file()
    assert GRANT.is_file()


def test_every_statement_can_run_twice() -> None:
    for statement in _statements(CREATE):
        assert re.match(
            r"(CREATE TABLE IF NOT EXISTS|CREATE INDEX IF NOT EXISTS|ALTER TABLE public\.agent_messages ADD COLUMN IF NOT EXISTS)",
            statement,
        ), statement


def test_the_migration_destroys_nothing() -> None:
    for path in (CREATE, GRANT):
        for statement in _statements(path):
            upper = statement.upper()
            for word in ("DROP ", "TRUNCATE", "DELETE ", "UPDATE ", "RENAME"):
                assert word not in upper, f"{path.name}: {statement[:80]}"


def test_every_relation_is_schema_qualified() -> None:
    for statement in _statements(CREATE):
        for rel in re.findall(
            r"(?:TABLE IF NOT EXISTS|\bON|ALTER TABLE)\s+([\w.]+)", statement
        ):
            assert rel.startswith("public."), statement


def test_the_key_is_partition_and_offset_so_a_replay_or_a_second_partition_is_decided_by_the_table() -> (
    None
):
    body = _create_table_body()
    assert re.search(r"PRIMARY KEY \(source_partition, source_offset\)", body), (
        "partition and offset together must be the primary key"
    )


def test_the_columns_match_what_the_writer_binds_and_use_no_reserved_word() -> None:
    body = _create_table_body()
    declared = []
    for line in body.splitlines():
        if line.strip().startswith(("PRIMARY KEY", "CONSTRAINT")):
            break
        if line.strip():
            declared.append(line.split()[0])
    assert declared == list(AGENT_MESSAGE_COLUMNS)
    assert not RESERVED & set(declared)


def test_the_sender_column_cannot_default_to_verified() -> None:
    body = _create_table_body()
    line = next(x for x in body.splitlines() if x.strip().startswith("sender_verified"))
    assert "NOT NULL" in line
    assert "DEFAULT FALSE" in line


def test_a_refused_row_must_say_why_and_a_readable_row_must_not() -> None:
    assert "agent_messages_refusal_reason_matches_status" in CREATE.read_text()


def test_the_grant_asserts_the_role_exists_and_grants_no_more_than_the_node_does() -> (
    None
):
    text = GRANT.read_text()
    assert "tenant_projection_writer" in text
    assert "GRANT SELECT, INSERT" in text
    assert "public.agent_messages" in text
    assert "DELETE" not in text.upper()
    assert "ALL PRIVILEGES" not in text.upper()
