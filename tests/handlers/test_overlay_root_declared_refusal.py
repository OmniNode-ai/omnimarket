# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule installers refuse an overlay root the lab host table does not declare (OMN-23711).

The installers pin the overlay root into the unit from whatever ``ONEX_SKILL_OVERLAY_ROOTS`` the
installing shell had, so three units on a lab host read a stale scratch clone. The install must
refuse a root that is not the overlays directory of the clone the lab host table declares for the
host. The table cases call the rule itself with each way a declaration can be wrong. The node cases run
each of the three installers' ``schedule install --dry-run`` from the node's example overlay: from the
declared root it succeeds, from a scratch root holding the same table it exits 1 and names the declared
root (without the refusal the scratch install exits 0 and pins the scratch root).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.rules_overlay_root_declared import (
    OMNI_HOME_VARIABLE,
    OVERLAYS_REF,
    OVERLAYS_REPO,
    OverlayRootRefusedError,
    declared_overlay_root,
    refuse_undeclared_overlay_root,
)
from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostClone

pytestmark = pytest.mark.unit

NODES = Path(importlib.import_module("omnimarket.nodes").__file__).resolve().parent

NODES_UNDER_TEST = (
    "node_code_graph_effect",
    "node_seam_deadline_probe_effect",
    "node_overlay_drift_effect",
)

HOST = "lab-host-x"


def _table() -> dict[str, Any]:
    return {
        "hosts": [
            {
                "name": HOST,
                "clones": [
                    {
                        "repo": OVERLAYS_REPO,
                        "ref": OVERLAYS_REF,
                        "path": f"{OMNI_HOME_VARIABLE}/overlays",
                    }
                ],
            }
        ]
    }


def _write_table(root: Path, document: dict[str, Any]) -> None:
    table = root / "lab_hosts" / "overlay.yaml"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text(yaml.safe_dump(document), encoding="utf-8")


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            {
                "hosts": [
                    {
                        "name": "other-host",
                        "clones": [
                            {
                                "repo": OVERLAYS_REPO,
                                "ref": OVERLAYS_REF,
                                "path": f"{OMNI_HOME_VARIABLE}/overlays",
                            }
                        ],
                    }
                ]
            },
            "no row for this host",
        ),
        ({"hosts": [{"name": HOST, "clones": []}]}, "no clones"),
        (
            {
                "hosts": [
                    {
                        "name": HOST,
                        "clones": [
                            {
                                "repo": OVERLAYS_REPO,
                                "ref": OVERLAYS_REF,
                                "path": f"{OMNI_HOME_VARIABLE}/overlays",
                            },
                            {
                                "repo": OVERLAYS_REPO,
                                "ref": OVERLAYS_REF,
                                "path": f"{OMNI_HOME_VARIABLE}/overlays",
                            },
                        ],
                    }
                ],
            },
            "more than one",
        ),
        (
            {
                "hosts": [
                    {
                        "name": HOST,
                        "clones": [
                            {
                                "repo": OVERLAYS_REPO,
                                "ref": "dev",
                                "path": f"{OMNI_HOME_VARIABLE}/overlays",
                            }
                        ],
                    }
                ]
            },
            "not 'main'",
        ),
    ],
    ids=[
        "host-row-missing",
        "row-with-no-clones",
        "two-overlays-clones",
        "clone-at-ref-dev",
    ],
)
@pytest.mark.parametrize("omni", ["omni"], ids=["omni"])
def test_declared_root_refuses_the_wrong_table(
    tmp_path: Path, document: dict[str, Any], message: str, omni: str
) -> None:
    root = tmp_path / omni / "overlays" / "overlays"
    _write_table(root, document)
    with pytest.raises(OverlayRootRefusedError, match=message):
        declared_overlay_root(document, HOST, tmp_path / omni)


def test_declared_root_is_the_overlays_directory(tmp_path: Path) -> None:
    omni = tmp_path / "omni"
    root = omni / "overlays" / "overlays"
    _write_table(root, _table())
    assert declared_overlay_root(_table(), HOST, omni) == root


def test_refuse_returns_none_for_the_declared_root(tmp_path: Path) -> None:
    omni = tmp_path / "omni"
    root = omni / "overlays" / "overlays"
    _write_table(root, _table())
    assert refuse_undeclared_overlay_root(root, host=HOST, omni_root=omni) is None


def test_refuse_names_the_file_when_the_table_is_absent(tmp_path: Path) -> None:
    omni = tmp_path / "omni"
    root = omni / "overlays" / "overlays"
    root.mkdir(parents=True)
    with pytest.raises(OverlayRootRefusedError, match=r"overlay\.yaml"):
        refuse_undeclared_overlay_root(root, host=HOST, omni_root=omni)


def test_refuse_names_both_roots_when_the_pinned_root_is_another_directory(
    tmp_path: Path,
) -> None:
    omni = tmp_path / "omni"
    declared = omni / "overlays" / "overlays"
    _write_table(declared, _table())
    scratch = tmp_path / "scratch" / "overlays-clone" / "overlays"
    _write_table(scratch, _table())
    with pytest.raises(OverlayRootRefusedError, match="scratch") as excinfo:
        refuse_undeclared_overlay_root(scratch, host=HOST, omni_root=omni)
    assert str(declared) in str(excinfo.value)


SCHEDULE_RUNTIMES = {
    "node_code_graph_effect": "runtime_code_graph",
    "node_seam_deadline_probe_effect": "runtime_seam_deadline",
    "node_overlay_drift_effect": "runtime_overlay_drift",
}


#: Where each node's overlay keeps the environment its installed unit carries (the overlays change of OMN-23710
#: declares ONEX_SKILL_OVERLAY_ROOTS there).
UNIT_ENVIRONMENT_OF = {
    "node_code_graph_effect": lambda document: document.setdefault(
        "unit_environment", {}
    ),
    "node_seam_deadline_probe_effect": lambda document: document["schedule_hosts"][
        0
    ].setdefault("environment", {}),
    "node_overlay_drift_effect": lambda document: document.setdefault(
        "unit_environment", {}
    ),
}
ROOTS_VARIABLE = "ONEX_SKILL_OVERLAY_ROOTS"


def _install_root(node: str, root: Path, declare: str | None = None) -> None:
    """The node's example overlay, renamed to the table's host, and the table, under one overlay root.

    ``declare`` is a value the overlay itself gives ONEX_SKILL_OVERLAY_ROOTS in its unit environment.
    """
    document = yaml.safe_load(
        (NODES / node / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    document["schedule_hosts"][0]["name"] = HOST
    if declare is not None:
        UNIT_ENVIRONMENT_OF[node](document)[ROOTS_VARIABLE] = declare
    overlay = root / node / "overlay.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(yaml.safe_dump(document), encoding="utf-8")
    _write_table(root, _table())


def _dry_run_install(
    node: str, root: Path, omni: Path, monkeypatch: pytest.MonkeyPatch
) -> int:
    runtime = importlib.import_module(
        f"omnimarket.nodes.{node}.{SCHEDULE_RUNTIMES[node]}"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return int(
        runtime._schedule(
            ["install", "--host", HOST, "--workspace-root", str(omni), "--dry-run"]
        )
    )


@pytest.mark.parametrize("node", NODES_UNDER_TEST, ids=NODES_UNDER_TEST)
def test_golden_a_dry_run_install_from_the_declared_root_succeeds(
    node: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omni = tmp_path / "omni"
    declared = omni / "overlays" / "overlays"
    _install_root(node, declared)
    assert _dry_run_install(node, declared, omni, monkeypatch) == 0


@pytest.mark.parametrize("node", NODES_UNDER_TEST, ids=NODES_UNDER_TEST)
def test_error_a_dry_run_install_from_a_scratch_root_is_refused_naming_the_declared_root(
    node: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    omni = tmp_path / "omni"
    declared = omni / "overlays" / "overlays"
    scratch = tmp_path / "scratch" / "overlays-clone" / "overlays"
    _install_root(node, scratch)
    assert _dry_run_install(node, scratch, omni, monkeypatch) == 1
    assert str(declared) in capsys.readouterr().err


@pytest.mark.parametrize("node", NODES_UNDER_TEST, ids=NODES_UNDER_TEST)
def test_seam_the_overlays_own_declaration_is_the_root_the_rendered_unit_carries(
    node: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The seam with the overlays repository: the key it declares (OMN-23710) is the key the renderer honours.

    A renderer that stopped letting the overlay's unit environment win over the installing shell's root, or an
    overlay that moved the key, would put the shell's root back in the unit; this fails then.
    """
    omni = tmp_path / "omni"
    declared = omni / "overlays" / "overlays"
    from_overlay = tmp_path / "the-overlays-own-declaration"
    _install_root(node, declared, declare=str(from_overlay))
    assert _dry_run_install(node, declared, omni, monkeypatch) == 0
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if f"{ROOTS_VARIABLE}=" in line
    ]
    assert lines
    assert all(
        line == f'Environment="{ROOTS_VARIABLE}={from_overlay}"' for line in lines
    )


def test_seam_the_table_shape_the_rule_reads_is_the_hosts_model_shape() -> None:
    """The seam with the lab host table: the clone cells the rule reads are the host model's own fields."""
    for host in _table()["hosts"]:
        for clone in host["clones"]:
            ModelLabHostClone.model_validate(clone)
