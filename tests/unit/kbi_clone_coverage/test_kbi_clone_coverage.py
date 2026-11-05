# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host's declared clone is refreshed from the host table, wherever it sits (OMN-22149).

Fixture: ``tests/fixtures/kbi_clone_coverage/h101_layout.yaml``, the recorded h101 layout (a clone at
the home ``Code`` directory, outside the registry-root scan). Real git repositories, a transport that
gives each host its own HOME.
"""

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolBusMessage
from omnimarket.nodes.node_canonical_clone_refresh_effect import (
    CanonicalCloneRefreshHost,
    HandlerCanonicalCloneRefreshEffect,
    ModelRepoMerged,
    load_canonical_clone_refresh_topics,
    publish_repo_merged,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
    HandlerDeclaredCloneRefresh,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_canonical_clone_refresh import (
    ModelCanonicalCloneRefreshRequest,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
    EnumDeclaredCloneRefusal,
    EnumDeclaredCloneState,
    ModelDeclaredCloneRefreshRequest,
)
from tests.helpers.kbi_clone_coverage_layout import HomesTransport, Layout, git

pytestmark = pytest.mark.unit


@pytest.fixture
def declared_hosts_file(
    layout: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    raw = layout.table.model_dump(mode="json")
    for row in raw["hosts"]:
        row["clones"][0]["path"] = str(layout.clone_path(row["name"]))
    hosts_file = tmp_path / "hosts.yaml"
    hosts_file.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(hosts_file))
    return hosts_file


@pytest.mark.parametrize("host_name", ["h101", "h053", "h051"])
def test_merge_consumer_refreshes_the_declared_clone_when_scan_finds_none(
    layout: Layout, declared_hosts_file: Path, host_name: str
) -> None:
    """The installed serve path must use the declared handler after an empty scan."""
    assert layout.scan_based_refresh("h101") == []

    async def scenario() -> None:
        bus = EventBusInmemory(environment="local", group="declared-merge-test")
        await bus.start()
        handler = HandlerCanonicalCloneRefreshEffect(
            [sys.executable, "-c", "pass", "{repo}"], host_name=host_name
        )
        host = CanonicalCloneRefreshHost(bus, handler)
        receipts: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def on_receipt(message: ProtocolBusMessage) -> None:
            receipts.put_nowait(json.loads(message.value)["payload"])

        unsubscribe = await bus.subscribe(
            load_canonical_clone_refresh_topics().receipt,
            on_message=on_receipt,
            group_id="declared-merge-probe",
        )
        await host.start()
        try:
            await publish_repo_merged(
                bus,
                ModelRepoMerged(
                    repo=layout.spec["origin_slug"],
                    base=layout.spec["ref"],
                    merge_sha=layout.origin_tip,
                    pr_number=1,
                    merged_at=datetime.now(UTC),
                    source="test",
                ),
            )
            receipt = await asyncio.wait_for(receipts.get(), timeout=10)
            expected = "up_to_date" if host_name == "h051" else "advanced"
            assert receipt["status"] == expected
            assert receipt["before_sha"] == layout.start[host_name]
            assert receipt["after_sha"] == layout.origin_tip
            assert layout.head(host_name) == layout.origin_tip
            for other in layout.start.keys() - {host_name}:
                assert layout.head(other) == layout.start[other]
        finally:
            await host.stop()
            await unsubscribe()
            await bus.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("case", "expected_status", "detail"),
    [
        ("missing_path", "refused", "path_missing"),
        ("diverged", "refused", "not_fast_forwarded"),
        ("missing_host", "refused", "absent from the declared host table"),
        ("missing_table", "refused", "coverage unreadable"),
        ("invalid_table", "refused", "coverage unreadable"),
        ("other_owner", "no_clone", "exact repository"),
        ("scan_failed", "failed", ""),
        ("explicit_no_clone", "advanced", "declared clone"),
    ],
)
def test_merge_fallback_preserves_refusals_and_exact_host_repository_scope(
    layout: Layout,
    declared_hosts_file: Path,
    case: str,
    expected_status: str,
    detail: str,
) -> None:
    host_name = "h101"
    repo = layout.spec["origin_slug"]
    clone = layout.clone_path(host_name)
    code = "pass"
    if case == "missing_path":
        clone.rename(clone.with_name("saved-clone"))
    elif case == "diverged":
        (clone / "local.txt").write_text("local\n")
        git(clone, "add", "local.txt")
        git(clone, "commit", "--quiet", "-m", "local only")
    elif case == "missing_host":
        host_name = "h999"
    elif case == "missing_table":
        declared_hosts_file.unlink()
    elif case == "invalid_table":
        declared_hosts_file.write_text("hosts: not-a-table\n")
    elif case == "other_owner":
        raw = yaml.safe_load(declared_hosts_file.read_text())
        for row in raw["hosts"]:
            row["clones"][0]["repo"] = f"OtherOwner/{layout.repo_dir}"
        declared_hosts_file.write_text(yaml.safe_dump(raw))
    elif case == "scan_failed":
        code = "raise SystemExit(1)"
    elif case == "explicit_no_clone":
        code = "print('NO_CLONE')"
    handler = HandlerCanonicalCloneRefreshEffect(
        [sys.executable, "-c", code, "{repo}"], host_name=host_name
    )
    receipt = handler.handle(
        ModelCanonicalCloneRefreshRequest(
            repo=repo, target_sha=layout.origin_tip, events=2
        )
    )
    assert receipt.status.value == expected_status
    assert detail in receipt.message
    assert receipt.events_coalesced == 2
    if case not in {"missing_path", "diverged", "explicit_no_clone"}:
        assert layout.head("h101") == layout.start["h101"]
    assert layout.head("h053") == layout.start["h053"]


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


def test_kbi_clone_coverage_declared_command_is_listed_by_the_adapter_module_entry() -> (
    None
):
    """The adapter module is the ``python -m`` entry of the whole group: ``declared`` is listed beside the others."""
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.adapter_declared_clone_refresh_host",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    commands = {
        line.split()[0]
        for line in done.stdout.split("Commands:")[-1].splitlines()
        if line.strip()
    }
    assert {"declared", "publish", "serve"} <= commands
