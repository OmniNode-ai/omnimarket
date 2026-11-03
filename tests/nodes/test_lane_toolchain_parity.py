# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22414: a lab host with part of the lane toolchain fails the parity read, by tool name.

Measured 2026-11-03 on the lab hosts: the bare ``onex`` on PATH raised ``ModuleNotFoundError: No module named 'omnibase_core'``
(h101, three ledger rows), pyright-python's ``nodeenv`` directory was empty and its install exited 1 (h053, two rows, so the
pyright commit hook blocked every commit staging Python), and neither h051 nor h053 had node or pnpm (a lab run printed
``command not found`` and exited 0). The declared toolchain per host is data (``lane_toolchain.yaml``); the parity read judges a
host's measured probes against it and names each missing or broken tool. Nothing here reads a credential.
"""

from __future__ import annotations

import inspect

import pytest
import yaml

from omnimarket.models.lane_toolchain.model_lane_toolchain_table import (
    ModelLaneToolchainTable,
)
from omnimarket.nodes.node_host_capability_compute.handlers import (
    handler_lane_toolchain,
)
from omnimarket.nodes.node_host_capability_compute.handlers.handler_lane_toolchain import (
    TABLE_PATH,
    HandlerLaneToolchain,
)
from omnimarket.nodes.node_host_capability_compute.models import (
    ModelHostToolchainReading,
    ModelLaneToolchainRequest,
    ModelToolReading,
)

pytestmark = pytest.mark.unit

NODEENV = ".cache/pyright-python/nodeenv"


def committed_table() -> ModelLaneToolchainTable:
    return ModelLaneToolchainTable.from_yaml_text(
        TABLE_PATH.read_text(encoding="utf-8")
    )


def judge(
    *readings: ModelHostToolchainReading, table: ModelLaneToolchainTable | None = None
) -> list[tuple[str, str, str]]:
    result = HandlerLaneToolchain().handle(
        ModelLaneToolchainRequest(
            table=table or committed_table(), readings=tuple(readings)
        )
    )
    return [(f.host, f.tool, f.state) for f in result.failures]


def ok(tool: str) -> ModelToolReading:
    return ModelToolReading(tool=tool, status=0, tail="")


def test_lane_toolchain_parity_names_the_broken_onex_and_the_empty_nodeenv() -> None:
    # The recorded failures, as one fixture host: bare onex raises ModuleNotFoundError, pyright's nodeenv directory is empty.
    host = ModelHostToolchainReading(
        host="h101",
        tools=(
            ModelToolReading(
                tool="onex",
                status=1,
                tail="ModuleNotFoundError: No module named 'omnibase_core'",
            ),
            ok("node"),
            ok("pnpm"),
            ModelToolReading(tool="pyright", status=1, tail="nodeenv: exit 1"),
        ),
        empty_dirs=(NODEENV,),
    )
    result = HandlerLaneToolchain().handle(
        ModelLaneToolchainRequest(table=committed_table(), readings=(host,))
    )
    assert [(f.host, f.tool, f.state) for f in result.failures] == [
        ("h101", "onex", "broken"),
        ("h101", "pyright", "broken"),
    ]
    by_tool = {f.tool: f.reason for f in result.failures}
    assert "ModuleNotFoundError" in by_tool["onex"]
    assert NODEENV in by_tool["pyright"]
    assert "holds no file" in by_tool["pyright"]


def test_lane_toolchain_parity_names_a_missing_node_and_pnpm() -> None:
    host = ModelHostToolchainReading(
        host="h051",
        tools=(
            ok("onex"),
            ModelToolReading(tool="node", status=127, tail="FileNotFoundError"),
            ModelToolReading(tool="pnpm", status=127, tail="FileNotFoundError"),
            ok("pyright"),
        ),
    )
    assert judge(host) == [("h051", "node", "missing"), ("h051", "pnpm", "missing")]


def test_lane_toolchain_parity_command_not_found_with_exit_zero_is_missing() -> None:
    # 2026-11-03: a lab run printed "command not found" and exited 0; the exit status alone is not the reading.
    host = ModelHostToolchainReading(
        host="h053",
        tools=(
            ModelToolReading(tool="node", status=0, tail="sh: node: command not found"),
        ),
    )
    assert judge(host) == [("h053", "node", "missing")]


def test_lane_toolchain_parity_a_complete_host_and_an_unmeasured_host_pass() -> None:
    table = committed_table()
    host = next(iter(table.hosts))
    complete = ModelHostToolchainReading(
        host=host, tools=tuple(ok(t) for t in table.hosts[host])
    )
    assert judge(complete) == []
    assert judge(ModelHostToolchainReading(host=host, tools=())) == []


def test_lane_toolchain_parity_a_host_the_table_does_not_declare_fails() -> None:
    assert judge(ModelHostToolchainReading(host="h999", tools=(ok("onex"),))) == [
        ("h999", "declaration", "undeclared")
    ]


def test_lane_toolchain_parity_every_committed_host_declares_the_four_tools() -> None:
    table = committed_table()
    assert {"h051", "h053", "h101"} <= set(table.hosts)
    for host, tools in table.hosts.items():
        assert {"onex", "node", "pnpm", "pyright"} <= set(tools), host


def test_lane_toolchain_parity_contract_data_the_toolchain_is_declared_not_coded() -> (
    None
):
    raw = yaml.safe_load(TABLE_PATH.read_text(encoding="utf-8"))
    for name, spec in raw["tools"].items():
        assert spec["probe"], name
        assert spec["repair"]["kind"] in {"lane-link", "clear-empty-tree", "install"}
    # No tool name or host name is a literal of the judging code.
    source = inspect.getsource(handler_lane_toolchain)
    for literal in (*raw["tools"], *raw["hosts"]):
        assert f'"{literal}"' not in source, literal
        assert f"'{literal}'" not in source, literal
    # A tool added to the data is judged with no code change.
    extended = ModelLaneToolchainTable.from_yaml_text(
        yaml.safe_dump(
            {
                "tools": {
                    **raw["tools"],
                    "zzadded": {
                        "probe": ["zzadded", "--version"],
                        "repair": {"kind": "install"},
                        "owner_fix": "the host owner installs zzadded",
                    },
                },
                "hosts": {"hx": [*raw["hosts"]["h101"], "zzadded"]},
            }
        )
    )
    reading = ModelHostToolchainReading(
        host="hx",
        tools=(ModelToolReading(tool="zzadded", status=127, tail="not found"),),
    )
    assert judge(reading, table=extended) == [("hx", "zzadded", "missing")]
