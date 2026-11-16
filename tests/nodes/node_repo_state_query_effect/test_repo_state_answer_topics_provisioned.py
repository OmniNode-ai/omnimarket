# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The repo-state answer topics are provisioned at boot (OMN-24612).

`repo-state query` publishes its request on the installer contract's
repo-state-query-requested command topic and waits, group-less, for the answer on the completed
or failed topic, timing out after 30 s with "Topic ... not found in cluster metadata" when the
topic does not exist. The dev runtime's per-contract boot provisioning creates only the topics
_contract_provision_topics returns (subscribe, publish, dlq and projection_api topics), and the
repo-state serve contract declares no event_bus block, so nothing creates the two answers. The
node_host_installer_registry_units contract lists them (OMN-23991's pattern) beside the two serve
command topics; the control test is the one that fails the whole fix if the repo-state
contract ever provisions its own answers alone.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths

# The wired-contract boot set includes subscribe, publish, DLQ and projection topics.
# The public boot_topic_provisioning.contract_publish_topics returns only publish
# topics, so it cannot replace this seam. No public full-set entry point is exported;
# an upstream rename fails collection and signals migration to a public equivalent.
from omnibase_infra.runtime.auto_wiring.handler_wiring import _contract_provision_topics

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
REPO_STATE_DIR = NODES / "node_repo_state_query_effect"
HOST_INSTALLER_DIR = NODES / "node_host_installer_registry_units"


def _contract(node_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((node_dir / "contract.yaml").read_text(encoding="utf-8"))


def _boot_provision_topics(node_dir: Path) -> set[str]:
    """The topics the dev runtime's boot provisioning creates for one node contract."""
    manifest = discover_contracts_from_paths([node_dir / "contract.yaml"])
    # A discovery that finds nothing must fail here, not return an empty set that a control then accepts.
    assert len(manifest.contracts) == 1, f"no contract discovered at {node_dir}"
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))
    return provisioned


def _installer_boot_provision_topics() -> set[str]:
    return _boot_provision_topics(HOST_INSTALLER_DIR)


@pytest.mark.parametrize("role", ["success", "failure"])
def test_each_answer_topic_is_boot_provisioned(role: str) -> None:
    terminal = _contract(REPO_STATE_DIR)["runtime_dispatch"]["terminal_events"]

    assert terminal[role] in _installer_boot_provision_topics()


def test_answer_topics_equal_the_published_events() -> None:
    contract = _contract(REPO_STATE_DIR)
    terminal = set(contract["runtime_dispatch"]["terminal_events"].values())
    published = {event["topic"] for event in contract["published_events"]}

    assert terminal == published


def test_answer_topics_are_declared_externally_consumed_and_never_subscribed() -> None:
    installer = _contract(HOST_INSTALLER_DIR)
    terminal = set(
        _contract(REPO_STATE_DIR)["runtime_dispatch"]["terminal_events"].values()
    )

    assert terminal <= set(installer["externally_consumed_topics"])
    assert not terminal & set(installer["event_bus"]["subscribe_topics"])


def test_the_repo_state_contract_alone_provisions_nothing() -> None:
    # The repo-state serve contract declares no event_bus block, so its boot provisioning
    # returns no topics at all; this is why the installer contract lists the two answers
    # beside its serve command topics.
    assert _boot_provision_topics(REPO_STATE_DIR) == set(), (
        "the repo-state contract now declares its own publish_topics, which is the truthful "
        "declaration; follow-up: remove the two answer rows from the installer contract's "
        "event_bus publish_topics, broker_acl_grants and externally_consumed_topics, and drop this control"
    )


class _NoContracts:
    contracts: tuple[object, ...] = ()


def test_a_discovery_that_finds_no_contract_fails_the_boot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two reads above and the control below loop over the discovered contracts; with no contract
    # the loop never runs and the control's empty set would pass without asking the runtime anything.
    monkeypatch.setattr(
        f"{__name__}.discover_contracts_from_paths", lambda _paths: _NoContracts()
    )

    with pytest.raises(AssertionError):
        _boot_provision_topics(HOST_INSTALLER_DIR)
    with pytest.raises(AssertionError):
        _boot_provision_topics(REPO_STATE_DIR)


def _answer_topics() -> set[str]:
    return set(
        _contract(REPO_STATE_DIR)["runtime_dispatch"]["terminal_events"].values()
    )


def _gate_over_installer_contract(
    tmp_path: Path, *, drop_grants: Any = None
) -> subprocess.CompletedProcess[str]:
    """Exercise the command's real discovery, grant aggregation and pinned git base.

    Copy every node contract, so another node's answer grants cannot mask removal
    from the installer. Only the installer grants are changed by the controls.
    The git pointer preserves the real pre-change base without synthesizing uses.
    """
    repo_root = NODES.parents[2]
    nodes_root = tmp_path / "src" / "omnimarket" / "nodes"
    for source in NODES.rglob("contract.yaml"):
        destination = nodes_root / source.relative_to(NODES)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, destination)
    git_path = repo_root / ".git"
    git_dir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=repo_root,
        env=scrub_git_location_env(os.environ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert git_path.exists(), "the command's pinned base needs the repository history"
    (tmp_path / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    contract_path = nodes_root / HOST_INSTALLER_DIR.name / "contract.yaml"
    if drop_grants is not None:
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        kept = drop_grants(contract.pop("broker_acl_grants", []))
        if kept is not None:
            contract["broker_acl_grants"] = kept
        contract_path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    # Invoke the public command: it constructs the request, filters TOPIC rows,
    # reads the pinned base and maps covered/refused to exit 0/1 itself.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_broker_acl_reconcile_effect.runtime_broker_acl_coverage_gate",
            "--base-ref",
            "f611a64d22e0",
            "--repo-root",
            str(tmp_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_the_gate_covers_the_two_added_answer_uses(tmp_path: Path) -> None:
    command = _gate_over_installer_contract(tmp_path)

    assert command.returncode == 0, command.stdout + command.stderr
    assert command.stdout.strip() == (
        "broker permission coverage: covered; 2 topic uses added since "
        "f611a64d22e0, 0 with no declared grant"
    )


def test_the_gate_refuses_the_installer_contract_without_its_grants_block(
    tmp_path: Path,
) -> None:
    command = _gate_over_installer_contract(tmp_path, drop_grants=lambda _block: None)

    assert command.returncode == 1, command.stdout + command.stderr
    for topic in _answer_topics():
        assert topic in command.stdout
    assert "refused; 2 topic uses added" in command.stdout
    assert "2 with no declared grant" in command.stdout


@pytest.mark.parametrize("operation", ["WRITE", "DESCRIBE"])
def test_the_gate_accepts_nothing_narrower_than_write_and_describe_for_a_publish_entry(
    tmp_path: Path, operation: str
) -> None:
    def without(block: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **principal,
                "grants": [
                    g for g in principal["grants"] if g["operation"] != operation
                ],
            }
            for principal in block
        ]

    command = _gate_over_installer_contract(tmp_path, drop_grants=without)

    assert command.returncode == 1, command.stdout + command.stderr
    assert "refused; 2 topic uses added" in command.stdout
    assert "2 with no declared grant" in command.stdout


def test_the_gate_refuses_create_in_place_of_write(tmp_path: Path) -> None:
    def create_instead(block: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **principal,
                "grants": [
                    {**grant, "operation": "CREATE"}
                    if grant["operation"] == "WRITE"
                    else grant
                    for grant in principal["grants"]
                ],
            }
            for principal in block
        ]

    command = _gate_over_installer_contract(tmp_path, drop_grants=create_instead)

    assert command.returncode == 1, command.stdout + command.stderr
    assert "refused; 2 topic uses added" in command.stdout
    assert "2 with no declared grant" in command.stdout
