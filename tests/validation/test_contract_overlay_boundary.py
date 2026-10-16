from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from scripts.validation.validate_contract_overlay_boundary import (
    main,
    validate_contract_overlay_boundary,
)

_DIRTY = """
name: node_example
model_routing:
  primary: qwen3-coder-30b
"""
_CLEAN = """
name: node_example
description: a clean contract
"""


def _write_contract(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_fails_model_defaults_in_node_contract(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
model_routing:
  primary: qwen3-coder-30b
""",
    )

    findings = validate_contract_overlay_boundary(tmp_path)

    assert len(findings) == 1
    assert findings[0].reason == "FORBIDDEN_MODEL_LITERAL"
    assert findings[0].yaml_path == "model_routing.primary"


def test_fails_served_model_id_key_even_with_unknown_value(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
config:
  served_model_id: runtime-owned
""",
    )

    findings = validate_contract_overlay_boundary(tmp_path)

    assert len(findings) == 1
    assert findings[0].reason == "FORBIDDEN_MODEL_OR_ENDPOINT_KEY"
    assert findings[0].yaml_path == "config.served_model_id"


def test_fails_llm_endpoint_literal(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
config:
  endpoint: http://llm-endpoint.example.invalid:8000
""",
    )

    findings = validate_contract_overlay_boundary(tmp_path)

    assert len(findings) == 1
    assert findings[0].reason == "FORBIDDEN_ENDPOINT_LITERAL"


def test_fails_provider_endpoint_literal_without_port(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
config:
  endpoint: https://api.openai.com/v1
""",
    )

    findings = validate_contract_overlay_boundary(tmp_path)

    assert len(findings) == 1
    assert findings[0].reason == "FORBIDDEN_ENDPOINT_LITERAL"


def test_fails_endpoint_literal_on_arbitrary_port(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
config:
  endpoint: http://llm-endpoint.example.invalid:9000
""",
    )

    findings = validate_contract_overlay_boundary(tmp_path)

    assert len(findings) == 1
    assert findings[0].reason == "FORBIDDEN_ENDPOINT_LITERAL"


def test_allows_policy_schema_without_defaults(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
model_routing_policy_schema:
  source: injected_ModelRoutingPolicy
  no_contract_model_defaults: true
  required_fields:
    - primary
    - timeout_per_attempt_s
""",
    )

    assert validate_contract_overlay_boundary(tmp_path) == []


def test_ignores_description_text(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
description: "Historical note mentions claude-sonnet but is not config."
""",
    )

    assert validate_contract_overlay_boundary(tmp_path) == []


def test_scoped_scan_reports_only_the_named_contracts(tmp_path: Path) -> None:
    dirty = _write_contract(
        tmp_path, "src/omnimarket/nodes/node_dirty/contract.yaml", _DIRTY
    )
    clean = _write_contract(
        tmp_path, "src/omnimarket/nodes/node_clean/contract.yaml", _CLEAN
    )

    assert [f.path for f in validate_contract_overlay_boundary(tmp_path)] == [dirty]
    assert validate_contract_overlay_boundary(tmp_path, paths=[clean]) == []
    scoped = validate_contract_overlay_boundary(tmp_path, paths=[dirty, clean])
    assert [f.path for f in scoped] == [dirty]


def test_scoped_scan_ignores_paths_that_are_not_node_contracts(
    tmp_path: Path,
) -> None:
    other = _write_contract(tmp_path, "docs/notes.yaml", _DIRTY)
    missing = tmp_path / "src/omnimarket/nodes/node_gone/contract.yaml"

    assert validate_contract_overlay_boundary(tmp_path, paths=[other, missing]) == []


def test_cli_positional_contract_fails_and_names_the_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dirty = _write_contract(
        tmp_path, "src/omnimarket/nodes/node_dirty/contract.yaml", _DIRTY
    )

    rc = main(["--root", str(tmp_path), str(dirty)])

    assert rc == 1
    assert "model_routing.primary" in capsys.readouterr().err


def test_cli_positional_clean_contract_passes_beside_a_dirty_one(
    tmp_path: Path,
) -> None:
    _write_contract(tmp_path, "src/omnimarket/nodes/node_dirty/contract.yaml", _DIRTY)
    clean = _write_contract(
        tmp_path, "src/omnimarket/nodes/node_clean/contract.yaml", _CLEAN
    )

    assert main(["--root", str(tmp_path), str(clean)]) == 0
    assert main(["--root", str(tmp_path)]) == 1


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def test_changed_since_scans_only_contracts_changed_after_the_ref(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _write_contract(
        tmp_path, "src/omnimarket/nodes/node_old_debt/contract.yaml", _DIRTY
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")

    # The debt that was already there is not re-reported ...
    assert main(["--root", str(tmp_path), "--changed-since", "HEAD"]) == 0

    # ... a contract that gains a model literal after the ref is.
    _write_contract(
        tmp_path, "src/omnimarket/nodes/node_new_debt/contract.yaml", _DIRTY
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "new")
    assert main(["--root", str(tmp_path), "--changed-since", "HEAD~1"]) == 1

    # Touching the old dirty contract makes its debt the author's to fix.
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_old_debt/contract.yaml",
        _DIRTY + "extra: 1\n",
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "touch")
    assert main(["--root", str(tmp_path), "--changed-since", "HEAD~1"]) == 1


def test_changed_since_refuses_an_unknown_ref(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")

    assert main(["--root", str(tmp_path), "--changed-since", "no-such-ref"]) == 2


def test_changed_since_ignores_a_git_dir_inherited_from_a_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    _write_contract(tmp_path, "src/omnimarket/nodes/node_new/contract.yaml", _DIRTY)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "new")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "no-such-git-dir"))

    assert main(["--root", str(tmp_path), "--changed-since", "HEAD~1"]) == 1
