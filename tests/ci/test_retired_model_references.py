# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The retired-model check covers history, all file types and its wiring."""

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_retired_model_reference_check_effect.handlers.handler_retired_model_reference_check import (
    violations,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("separator", ["-", "_", " ", "", "--", "_ "])
@pytest.mark.parametrize("suffix", [".py", ".yaml", ".json", ".md", ".txt"])
def test_new_reference_and_dated_comment_are_refused(tmp_path, separator, suffix):
    path = tmp_path / ("history" + suffix)
    name = "GPT" + separator + "OSS"
    path.write_text(f"# 2026-10-02: {name}-120b served here\n")
    assert violations(tmp_path, [path]) == [
        f"history{suffix}:1: retired-model reference"
    ]


def test_filename_is_checked_and_external_overlay_is_not_a_repo_exception(tmp_path):
    path = tmp_path / ("gpt" + "_oss_overlay.yaml")
    path.write_text("model: deployment-owned\n")
    assert violations(tmp_path, [path])


def test_neutral_record_is_allowed(tmp_path):
    path = tmp_path / "history.yaml"
    path.write_text("# retired lab rung: 0/28 adequate\n")
    assert violations(tmp_path, [path]) == []


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
@pytest.mark.parametrize("as_key", [False, True])
def test_escaped_keys_and_values_are_still_refused(tmp_path, suffix, as_key):
    path = tmp_path / ("escaped" + suffix)
    escaped = '"gpt' + r'\u002doss-120b"'
    path.write_text(
        "{" + escaped + ": true}" if as_key else '{"model": ' + escaped + "}"
    )
    assert violations(tmp_path, [path]) == [
        f"escaped{suffix}: retired-model reference in parsed content"
    ]


def test_check_runs_in_hook_and_required_ci_lint():
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "retired-model-references"
    )
    command = "python -m omnimarket.nodes.node_retired_model_reference_check_effect"
    assert command in hook["entry"]
    assert hook["always_run"] is True
    assert hook["pass_filenames"] is False
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert any(command in step.get("run", "") for step in ci["jobs"]["lint"]["steps"])


def test_cli_refuses_new_reference(tmp_path, monkeypatch):
    import subprocess

    from omnimarket.nodes.node_retired_model_reference_check_effect.__main__ import main
    from omnimarket.nodes.node_retired_model_reference_check_effect.handlers import (
        handler_retired_model_reference_check as check,
    )

    path = tmp_path / "new.md"
    path.write_text("gpt" + "-oss-120b\n")
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda *args, **_kwargs: subprocess.CompletedProcess(
            args[0], 0, b"new.md\0", b""
        ),
    )
    assert main(["--repo-root", str(tmp_path)]) == 1
    path.write_text("deployment-owned model\n")
    assert main(["--repo-root", str(tmp_path)]) == 0


def test_packaged_node_contract_dispatches_typed_request(tmp_path):
    import importlib
    from importlib.metadata import entry_points
    from importlib.resources import files

    node = "omnimarket.nodes.node_retired_model_reference_check_effect"
    assert node.rsplit(".", 1)[-1] in {
        entry.name for entry in entry_points(group="onex.nodes")
    }
    contract = yaml.safe_load(files(node).joinpath("contract.yaml").read_text())
    route = contract["handler_routing"]["handlers"][0]
    handler = getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )()
    input_spec = contract["input_model"]
    model = getattr(importlib.import_module(input_spec["module"]), input_spec["name"])
    source = tmp_path / "record.md"
    source.write_text("deployment-owned model\n")
    request = model(repo_root=str(tmp_path), paths=(source.name,))
    assert handler.handle(request).admitted is True
    source.write_text("gpt" + "-oss: dated history\n")
    result = handler.handle(request)
    assert result.admitted is False
    assert result.exit_code == 1
