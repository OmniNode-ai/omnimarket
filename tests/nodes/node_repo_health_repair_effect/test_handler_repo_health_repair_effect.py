# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_repo_health_repair_effect handler (OMN-13584).

TDD-first: tests are written against the contract/DoD before the implementation.

DoD evidence required:
  - Idempotency: two identical REPO_BASELINE inputs → one repair-emitted event /
    one ticket ref (content-key dedup asserted).
  - Integration: emitted event carries failing_command + classification_reason
    (baseline evidence) from the input.
  - Secret/token resolved via contract api_key_ref (no literal token in handler;
    mocked in all tests).
  - All tests are unit-isolated: Linear API calls are mocked via the
    LinearClientProtocol injection point.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from omnimarket.events.repo_health import (
    EnumFailureOrigin,
    ModelRepoHealthClassification,
)
from omnimarket.nodes.node_repo_health_repair_effect.handlers.handler_repo_health_repair import (
    HandlerRepoHealthRepairEffect,
)
from omnimarket.nodes.node_repo_health_repair_effect.models.model_repair_command import (
    ModelRepoHealthRepairCommand,
)
from omnimarket.nodes.node_repo_health_repair_effect.models.model_repair_emitted_event import (
    ModelRepoHealthRepairEmittedEvent,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CORR_ID = UUID("00000000-0000-4000-a000-000000000001")


def _classification(
    failing_command: str = "pre-commit run --all-files",
    failing_paths: tuple[str, ...] = ("src/foo.py", "tests/test_bar.py"),
) -> ModelRepoHealthClassification:
    """Build a minimal REPO_BASELINE classification."""
    return ModelRepoHealthClassification(
        origin=EnumFailureOrigin.REPO_BASELINE,
        reason=(
            "Failing path(s) are pre-existing on the dev baseline and not in "
            f"the PR changed set: {', '.join(failing_paths)}"
        ),
        matched_paths=failing_paths,
        correlation_id=_CORR_ID,
        repo="OmniNode-ai/omnimarket",
        pr_number=42,
        failing_command=failing_command,
    )


def _command(
    classification: ModelRepoHealthClassification | None = None,
    dry_run: bool = False,
) -> ModelRepoHealthRepairCommand:
    return ModelRepoHealthRepairCommand(
        correlation_id=_CORR_ID,
        classification=classification or _classification(),
        parent_issue_id="OMN-13316",
        dry_run=dry_run,
    )


class _MockLinearClient:
    """Minimal injectable Linear client for unit tests.

    Tracks create_issue calls and supports pre-seeded existing-ticket results.
    """

    def __init__(
        self,
        existing_tickets: dict[str, str] | None = None,
        created_ref: str = "OMN-99999",
    ) -> None:
        # content_key -> ticket_ref for pre-existing tickets
        self._existing: dict[str, str] = existing_tickets or {}
        self._created_ref = created_ref
        self.create_issue_calls: list[dict[str, Any]] = []
        self.search_calls: list[str] = []

    def search_issues_by_content_key(self, *, content_key: str) -> str | None:
        """Return pre-existing ticket ref if found, else None."""
        self.search_calls.append(content_key)
        return self._existing.get(content_key)

    def create_issue(self, *, title: str, description: str, parent_id: str) -> str:
        """Create a new ticket and return its identifier."""
        self.create_issue_calls.append(
            {"title": title, "description": description, "parent_id": parent_id}
        )
        return self._created_ref


# ---------------------------------------------------------------------------
# Helper: expected content key
# ---------------------------------------------------------------------------


def _expected_content_key(
    failing_command: str,
    failing_paths: tuple[str, ...],
) -> str:
    """Mirror the handler's dedup key derivation."""
    canonical = failing_command + "|" + "|".join(sorted(failing_paths))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Test: idempotency — two identical inputs → one ticket ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_same_input_twice_produces_one_ticket_ref() -> None:
    """Two identical REPO_BASELINE inputs → one ticket ref (idempotency key dedup).

    The first call creates a ticket; the second call discovers the same
    content_key in the Linear search and returns the existing ref without
    creating a second ticket. This asserts the core DoD idempotency guarantee.
    """
    cmd = _command()
    expected_key = _expected_content_key(
        cmd.classification.failing_command,
        cmd.classification.matched_paths,
    )

    # Simulate: first call has no pre-existing ticket; second call is seeded
    # with the key returned by the first.
    client_first_call = _MockLinearClient(existing_tickets={}, created_ref="OMN-77001")
    handler = HandlerRepoHealthRepairEffect(linear_client=client_first_call)
    event_first = await handler.handle(cmd)

    assert isinstance(event_first, ModelRepoHealthRepairEmittedEvent)
    assert event_first.ticket_created is True
    assert event_first.repair_ticket_ref == "OMN-77001"
    assert event_first.content_key == expected_key
    assert len(client_first_call.create_issue_calls) == 1  # exactly one create

    # Second call: content_key already exists → no new ticket created.
    client_second_call = _MockLinearClient(
        existing_tickets={expected_key: "OMN-77001"}, created_ref="OMN-99998"
    )
    handler_second = HandlerRepoHealthRepairEffect(linear_client=client_second_call)
    event_second = await handler_second.handle(cmd)

    assert isinstance(event_second, ModelRepoHealthRepairEmittedEvent)
    assert event_second.ticket_created is False
    assert event_second.repair_ticket_ref == "OMN-77001"  # same ref, not a new one
    assert event_second.content_key == expected_key
    assert len(client_second_call.create_issue_calls) == 0  # no second create


# ---------------------------------------------------------------------------
# Test: payload carries required fields (failing_command + baseline evidence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emitted_event_carries_failing_command_and_classification_reason() -> (
    None
):
    """Emitted event must carry the failing_command and classification_reason."""
    cls = _classification(
        failing_command="uv run ruff check src/",
        failing_paths=("src/omnimarket/nodes/old_node.py",),
    )
    cmd = _command(classification=cls)
    client = _MockLinearClient(created_ref="OMN-55500")
    handler = HandlerRepoHealthRepairEffect(linear_client=client)
    event = await handler.handle(cmd)

    assert event.failing_command == "uv run ruff check src/"
    assert event.classification_reason == cls.reason
    assert event.repo == "OmniNode-ai/omnimarket"
    assert event.pr_number == 42
    assert event.correlation_id == _CORR_ID


# ---------------------------------------------------------------------------
# Test: dry_run — computes key, does NOT create ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_does_not_create_ticket() -> None:
    """dry_run=True returns the content_key without calling create_issue."""
    cmd = _command(dry_run=True)
    client = _MockLinearClient(created_ref="OMN-00000")
    handler = HandlerRepoHealthRepairEffect(linear_client=client)
    event = await handler.handle(cmd)

    assert event.dry_run is True
    assert event.ticket_created is False
    assert event.repair_ticket_ref is None
    assert len(client.create_issue_calls) == 0  # no ticket created
    # Key must still be deterministic
    expected_key = _expected_content_key(
        cmd.classification.failing_command,
        cmd.classification.matched_paths,
    )
    assert event.content_key == expected_key


# ---------------------------------------------------------------------------
# Test: different inputs → different content keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_inputs_produce_different_content_keys() -> None:
    """Different failing command + path combinations must produce distinct keys."""
    cls_a = _classification(
        failing_command="pre-commit run --all-files",
        failing_paths=("src/a.py",),
    )
    cls_b = _classification(
        failing_command="uv run mypy src/",
        failing_paths=("src/b.py",),
    )
    key_a = _expected_content_key(cls_a.failing_command, cls_a.matched_paths)
    key_b = _expected_content_key(cls_b.failing_command, cls_b.matched_paths)
    assert key_a != key_b


# ---------------------------------------------------------------------------
# Test: content key is path-order-independent (sorted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_key_is_order_independent() -> None:
    """Sorting the failing_paths before hashing makes the key order-independent."""
    cls_forward = _classification(
        failing_paths=("src/a.py", "src/b.py", "src/c.py"),
    )
    cls_reversed = _classification(
        failing_paths=("src/c.py", "src/b.py", "src/a.py"),
    )
    # Both commands are the same default; only path order differs.
    key_fwd = _expected_content_key(
        cls_forward.failing_command, cls_forward.matched_paths
    )
    key_rev = _expected_content_key(
        cls_reversed.failing_command, cls_reversed.matched_paths
    )
    assert key_fwd == key_rev


# ---------------------------------------------------------------------------
# Test: Linear ticket description carries required evidence fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_linear_ticket_description_carries_evidence() -> None:
    """The Linear ticket description must contain failing_command + reason."""
    cls = _classification(
        failing_command="pre-commit run --all-files",
        failing_paths=("tests/test_old.py",),
    )
    cmd = _command(classification=cls)
    client = _MockLinearClient(created_ref="OMN-33300")
    handler = HandlerRepoHealthRepairEffect(linear_client=client)
    await handler.handle(cmd)

    assert len(client.create_issue_calls) == 1
    desc = client.create_issue_calls[0]["description"]
    assert "pre-commit run --all-files" in desc
    assert cls.reason in desc
    assert "OMN-13316" in client.create_issue_calls[0]["parent_id"]


# ---------------------------------------------------------------------------
# Test: no literal LINEAR_API_KEY in handler source
# ---------------------------------------------------------------------------


def test_no_literal_api_key_in_source() -> None:
    """Confirm the handler module does not contain a bare LINEAR_API_KEY string.

    The secret must be resolved exclusively via contract api_key_ref.
    """
    from pathlib import Path

    handler_src = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_repo_health_repair_effect"
        / "handlers"
        / "handler_repo_health_repair.py"
    )
    text = handler_src.read_text(encoding="utf-8")
    # The key name may appear as a reference string (e.g. in contract_secret_ref
    # calls or comments) but must never be used as a bare os.environ[] access.
    assert 'os.environ["LINEAR_API_KEY"]' not in text
    assert "os.environ['LINEAR_API_KEY']" not in text
    assert "os.getenv('LINEAR_API_KEY'" not in text
    assert 'os.getenv("LINEAR_API_KEY"' not in text


# ---------------------------------------------------------------------------
# Test: contract-level no-op — missing api_key_ref raises at resolution time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_api_key_raises_when_no_injectable_client() -> None:
    """When no client is injected, the handler raises if the secret is unset."""
    cmd = _command()
    # Patch the secret resolver to return None (missing secret)
    with patch(
        "omnimarket.nodes.node_repo_health_repair_effect.handlers.handler_repo_health_repair.resolve_api_key",
        return_value=None,
    ):
        handler = HandlerRepoHealthRepairEffect()
        with pytest.raises((RuntimeError, ValueError)):
            await handler.handle(cmd)
