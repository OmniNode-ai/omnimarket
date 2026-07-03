# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for the OMN-13854 gate-escape audit's real I/O boundary.

Mirrors the pre-existing convention in ``test_golden_chain_dod_sweep_orchestrator.py``
of mocking ``subprocess.run`` / the network call directly to prove the real
boundary implementations (as opposed to
``tests/integration/node_dod_sweep_orchestrator/test_dod_sweep_gate_escape_audit.py``,
which proves the handler wiring via constructor-injected fakes and never
touches these functions).

Covers:
  - ``_gh_search_merged_pr_exists``: hit, miss, non-zero exit, timeout/exception
  - ``_linear_graphql_post`` / ``_fetch_done_tickets_via_linear``: pagination,
    field mapping (startedAt/attachments/children -> snapshot fields), GraphQL
    error surfacing
  - ``_post_linear_comment``: request shape (never mutates ticket state)
"""

from __future__ import annotations

import json
import subprocess
import unittest.mock as mock

import pytest

from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    _fetch_done_tickets_via_linear,
    _gh_search_merged_pr_exists,
    _linear_graphql_post,
    _post_linear_comment,
)

# ---------------------------------------------------------------------------
# _gh_search_merged_pr_exists
# ---------------------------------------------------------------------------


def test_gh_search_merged_pr_exists_hit() -> None:
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=json.dumps([{"number": 42}]), stderr=""
        )
        found = _gh_search_merged_pr_exists("OMN-13854")

    assert found is True
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["gh", "search", "prs"]
    assert "OMN-13854" in cmd
    assert "--state" in cmd
    assert "merged" in cmd


def test_gh_search_merged_pr_exists_miss() -> None:
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="[]", stderr="")
        found = _gh_search_merged_pr_exists("OMN-99999")

    assert found is False


def test_gh_search_merged_pr_exists_nonzero_exit_is_false() -> None:
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="rate limited"
        )
        found = _gh_search_merged_pr_exists("OMN-13854")

    assert found is False


def test_gh_search_merged_pr_exists_timeout_is_false() -> None:
    with mock.patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=20),
    ):
        found = _gh_search_merged_pr_exists("OMN-13854")

    assert found is False


def test_gh_search_merged_pr_exists_malformed_json_is_false() -> None:
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="not json", stderr="")
        found = _gh_search_merged_pr_exists("OMN-13854")

    assert found is False


# ---------------------------------------------------------------------------
# _linear_graphql_post / _fetch_done_tickets_via_linear
# ---------------------------------------------------------------------------


def _http_response(payload: dict[str, object]) -> mock.Mock:
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    return response


def test_linear_graphql_post_raises_on_graphql_errors() -> None:
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _http_response(
            {"errors": [{"message": "bad query"}]}
        )
        with pytest.raises(RuntimeError, match="Linear GraphQL error"):
            _linear_graphql_post("query {}", {}, "fake-key")


def test_fetch_done_tickets_maps_fields_and_stops_without_next_page() -> None:
    page = {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": "uuid-1",
                        "identifier": "OMN-13797",
                        "title": "Wire node_release EFFECT nodes",
                        "startedAt": None,
                        "completedAt": "2026-07-02T05:19:00Z",
                        "state": {"name": "Done"},
                        "labels": {"nodes": []},
                        "attachments": {"nodes": []},
                        "children": {"nodes": []},
                    },
                    {
                        "id": "uuid-2",
                        "identifier": "OMN-13817",
                        "title": "Gate the receipt-minting close path",
                        "startedAt": "2026-07-02T03:20:52.190Z",
                        "completedAt": "2026-07-02T05:19:13Z",
                        "state": {"name": "Done"},
                        "labels": {"nodes": [{"name": "beta-blocker"}]},
                        "attachments": {"nodes": [{"id": "a1"}, {"id": "a2"}]},
                        "children": {"nodes": []},
                    },
                    {
                        "id": "uuid-3",
                        "identifier": "OMN-13674",
                        "title": "EFFECT coverage epic",
                        "startedAt": None,
                        "completedAt": "2026-07-02T00:00:00Z",
                        "state": {"name": "Done"},
                        "labels": {"nodes": []},
                        "attachments": {"nodes": []},
                        "children": {
                            "nodes": [
                                {"id": "c1", "state": {"name": "Done"}},
                                {"id": "c2", "state": {"name": "Cancelled"}},
                            ]
                        },
                    },
                ],
            }
        }
    }
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _http_response(page)
        snapshots = _fetch_done_tickets_via_linear(
            "Omninode", "2026-07-01", "2026-07-03", "fake-key"
        )

    assert mock_urlopen.call_count == 1
    assert {s.identifier for s in snapshots} == {"OMN-13797", "OMN-13817", "OMN-13674"}

    incident = next(s for s in snapshots if s.identifier == "OMN-13797")
    assert incident.started_at is None
    assert incident.attachments_count == 0
    assert incident.has_children is False

    evidenced = next(s for s in snapshots if s.identifier == "OMN-13817")
    assert evidenced.started_at == "2026-07-02T03:20:52.190Z"
    assert evidenced.attachments_count == 2
    assert evidenced.labels == ("beta-blocker",)

    epic = next(s for s in snapshots if s.identifier == "OMN-13674")
    assert epic.has_children is True
    assert epic.all_children_done is True


def test_fetch_done_tickets_paginates() -> None:
    page_1 = {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                "nodes": [
                    {
                        "id": "uuid-1",
                        "identifier": "OMN-90001",
                        "title": "t1",
                        "startedAt": None,
                        "completedAt": "2026-07-02T00:00:00Z",
                        "state": {"name": "Done"},
                        "labels": {"nodes": []},
                        "attachments": {"nodes": []},
                        "children": {"nodes": []},
                    }
                ],
            }
        }
    }
    page_2 = {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": "uuid-2",
                        "identifier": "OMN-90002",
                        "title": "t2",
                        "startedAt": None,
                        "completedAt": "2026-07-02T00:00:00Z",
                        "state": {"name": "Done"},
                        "labels": {"nodes": []},
                        "attachments": {"nodes": []},
                        "children": {"nodes": []},
                    }
                ],
            }
        }
    }
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [_http_response(page_1), _http_response(page_2)]
        snapshots = _fetch_done_tickets_via_linear("Omninode", "", "", "fake-key")

    assert mock_urlopen.call_count == 2
    assert {s.identifier for s in snapshots} == {"OMN-90001", "OMN-90002"}


# ---------------------------------------------------------------------------
# _post_linear_comment
# ---------------------------------------------------------------------------


def test_post_linear_comment_sends_comment_create_mutation() -> None:
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _http_response(
            {"data": {"commentCreate": {"success": True}}}
        )
        _post_linear_comment("uuid-1", "flagged as gate-escape candidate", "fake-key")

    request_obj = mock_urlopen.call_args[0][0]
    body = json.loads(request_obj.data.decode())
    assert "commentCreate" in body["query"]
    assert body["variables"] == {
        "issueId": "uuid-1",
        "body": "flagged as gate-escape candidate",
    }
    # Never a stateId/issueUpdate mutation — this function must never mutate state.
    assert "issueUpdate" not in body["query"]
    assert "stateId" not in body["query"]
