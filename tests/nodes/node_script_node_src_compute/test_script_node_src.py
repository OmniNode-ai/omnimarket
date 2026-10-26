"""The node source resolver picks the first candidate that defines every node name a script takes."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_script_node_src_compute.handler import HandlerScriptNodeSrc
from omnimarket.nodes.node_script_node_src_compute.models import (
    ModelScriptNodeSrcRequest,
)

SCRIPT = "from omniclaude_internal.nodes.node_example import handler\nhandler.run()\n"


def source(root: Path, defines: str) -> str:
    module = root / "omniclaude_internal" / "nodes" / "node_example"
    module.mkdir(parents=True)
    (module / "handler.py").write_text(defines)
    return str(root)


def resolve(script: Path, *candidates: str):
    return HandlerScriptNodeSrc().handle(
        ModelScriptNodeSrcRequest(str(script), candidates)
    )


def test_the_first_candidate_that_defines_everything_wins(tmp_path: Path) -> None:
    script = tmp_path / "script.py"
    script.write_text(
        "from omniclaude_internal.nodes.node_example.handler import run\n"
    )
    stale = source(tmp_path / "stale", "def other(): ...\n")
    current = source(tmp_path / "current", "def run(): ...\n")
    result = resolve(script, stale, current)
    assert result.src == current
    assert result.message == ""


def test_a_candidate_lacking_a_name_is_refused_naming_what_it_lacks(
    tmp_path: Path,
) -> None:
    script = tmp_path / "script.py"
    script.write_text(
        "from omniclaude_internal.nodes.node_example.handler import run\n"
    )
    stale = source(tmp_path / "stale", "def other(): ...\n")
    result = resolve(script, stale)
    assert result.src is None
    assert f"{stale} lacks" in result.message
    assert "run" in result.message


def test_no_candidate_with_the_package_names_the_registry_root_variable(
    tmp_path: Path,
) -> None:
    script = tmp_path / "script.py"
    script.write_text(SCRIPT)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = resolve(script, str(empty))
    assert result.src is None
    assert "ONEX_REGISTRY_ROOT" in result.message
