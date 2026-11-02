# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host's declared clone is refreshed from the host table, wherever it sits (OMN-22149).

Fixture: ``tests/fixtures/kbi_clone_coverage/h101_layout.yaml``, the recorded h101 layout (a clone at
the home ``Code`` directory, outside the registry-root scan). Real git repositories, a transport that
gives each host its own HOME.
"""

import pytest

from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
    HandlerDeclaredCloneRefresh,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
    EnumDeclaredCloneRefusal,
    EnumDeclaredCloneState,
    ModelDeclaredCloneRefreshRequest,
)
from tests.helpers.kbi_clone_coverage_layout import HomesTransport, Layout, git

pytestmark = pytest.mark.unit


def _handler(
    layout: Layout, unreachable: frozenset[str] = frozenset()
) -> HandlerDeclaredCloneRefresh:
    return HandlerDeclaredCloneRefresh(
        layout.table,
        layout.repo_dir,
        HomesTransport(layout.homes, unreachable=unreachable),
    )


def test_kbi_clone_coverage_scan_based_behaviour_leaves_the_h101_clone_behind(
    layout: Layout,
) -> None:
    """The failure the node replaces: the scan lists no clone for h101 and moves nothing."""
    assert layout.scan_based_refresh("h101") == []
    assert layout.head("h101") == layout.start["h101"] != layout.origin_tip


def test_kbi_clone_coverage_refreshes_every_declared_clone_outside_the_scan(
    layout: Layout,
) -> None:
    receipt = _handler(layout).handle(ModelDeclaredCloneRefreshRequest())
    by_host = {o.host: o for o in receipt.outcomes}
    assert set(by_host) == {"h101", "h053", "h051"}
    assert not receipt.refused
    for host in ("h101", "h053"):
        assert by_host[host].state is EnumDeclaredCloneState.REFRESHED
        assert by_host[host].before_sha == layout.start[host]
    assert by_host["h051"].state is EnumDeclaredCloneState.UP_TO_DATE
    for host, outcome in by_host.items():
        assert outcome.after_sha == outcome.origin_sha == layout.origin_tip
        assert layout.head(host) == layout.origin_tip
    assert by_host["h101"].path == "$HOME/Code/kbrepo"


def test_kbi_clone_coverage_second_run_moves_nothing(layout: Layout) -> None:
    handler = _handler(layout)
    handler.handle(ModelDeclaredCloneRefreshRequest())
    again = handler.handle(ModelDeclaredCloneRefreshRequest())
    assert {o.state for o in again.outcomes} == {EnumDeclaredCloneState.UP_TO_DATE}


def test_kbi_clone_coverage_names_only_the_requested_hosts(layout: Layout) -> None:
    receipt = _handler(layout).handle(ModelDeclaredCloneRefreshRequest(hosts=("h101",)))
    assert [o.host for o in receipt.outcomes] == ["h101"]
    assert layout.head("h053") == layout.start["h053"]


def test_kbi_clone_coverage_refuses_a_missing_path(layout: Layout) -> None:
    git(
        layout.homes["h101"], "init", "--quiet", "x"
    )  # unrelated repo; the declared path is gone
    import shutil

    shutil.rmtree(layout.clone_path("h101"))
    receipt = _handler(layout).handle(ModelDeclaredCloneRefreshRequest())
    bad = next(o for o in receipt.outcomes if o.host == "h101")
    assert receipt.refused
    assert bad.state is EnumDeclaredCloneState.REFUSED
    assert bad.refusal is EnumDeclaredCloneRefusal.PATH_MISSING
    assert bad.host == "h101"
    assert bad.path == "$HOME/Code/kbrepo"
    # one host's refusal does not stop the others
    assert layout.head("h053") == layout.origin_tip


def test_kbi_clone_coverage_refuses_a_directory_that_is_not_a_clone(
    layout: Layout,
) -> None:
    import shutil

    shutil.rmtree(layout.clone_path("h053") / ".git")
    receipt = _handler(layout).handle(ModelDeclaredCloneRefreshRequest())
    bad = next(o for o in receipt.outcomes if o.host == "h053")
    assert receipt.refused
    assert bad.refusal is EnumDeclaredCloneRefusal.NOT_A_CLONE


def test_kbi_clone_coverage_refuses_a_clone_that_cannot_fast_forward(
    layout: Layout,
) -> None:
    clone = layout.clone_path("h101")
    (clone / "local.txt").write_text("local\n")
    git(clone, "add", "local.txt")
    git(clone, "commit", "--quiet", "-m", "local only")
    receipt = _handler(layout).handle(ModelDeclaredCloneRefreshRequest())
    bad = next(o for o in receipt.outcomes if o.host == "h101")
    assert receipt.refused
    assert bad.refusal is EnumDeclaredCloneRefusal.NOT_FAST_FORWARDED
    assert bad.after_sha != bad.origin_sha


def test_kbi_clone_coverage_refuses_an_unreachable_host(layout: Layout) -> None:
    receipt = _handler(layout, unreachable=frozenset({"h053"})).handle(
        ModelDeclaredCloneRefreshRequest()
    )
    bad = next(o for o in receipt.outcomes if o.host == "h053")
    assert receipt.refused
    assert bad.refusal is EnumDeclaredCloneRefusal.HOST_UNREACHABLE
    assert "h053" in bad.message


def test_kbi_clone_coverage_refuses_a_named_host_that_declares_no_clone(
    layout: Layout,
) -> None:
    receipt = _handler(layout).handle(ModelDeclaredCloneRefreshRequest(hosts=("h999",)))
    assert receipt.refused
    assert receipt.outcomes[0].host == "h999"
    assert receipt.outcomes[0].refusal is EnumDeclaredCloneRefusal.NOT_DECLARED


def test_kbi_clone_coverage_an_empty_table_is_a_refusal_not_a_clean_run(
    layout: Layout,
) -> None:
    handler = HandlerDeclaredCloneRefresh(
        layout.table, "no-such-repo", HomesTransport(layout.homes)
    )
    assert handler.handle(ModelDeclaredCloneRefreshRequest()).refused
