"""Guard local incremental mypy scope and authoritative CI coverage."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.ci.ci_summary_gate import SKIPPABLE_GATE_JOBS

REPO_ROOT = Path(__file__).resolve().parents[2]


def _local_hook() -> dict[str, object]:
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    for repository in config["repos"]:
        for hook in repository.get("hooks", []):
            if hook.get("id") == "mypy-type-check":
                return hook
    raise AssertionError("mypy-type-check hook is missing")


def test_local_mypy_uses_only_outgoing_source_files() -> None:
    hook = _local_hook()
    entry = hook["entry"]

    assert hook["pass_filenames"] is True
    assert isinstance(entry, str)
    assert 'mypy --show-error-codes --no-error-summary -- "$@"' in entry
    assert hook["files"] == r"^src/omnimarket/.*\.py$"
    assert hook["stages"] == ["pre-push"]


def test_full_strict_mypy_remains_ci_summary_gated_for_code_changes() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "typecheck:" in workflow
    assert "needs.zone-filter.outputs.docs_only != 'true'" in workflow
    assert "uv run mypy src/omnimarket/ --strict" in workflow
    assert "typecheck" in SKIPPABLE_GATE_JOBS
