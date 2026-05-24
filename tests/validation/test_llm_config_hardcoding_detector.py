# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_DETECTOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "validation"
    / "detect_llm_config_hardcoding.py"
)


def _load_detector() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "detect_llm_config_hardcoding", _DETECTOR_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def detector() -> ModuleType:
    return _load_detector()


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_runtime_provider_construction_and_endpoint_invention_are_blocking(
    tmp_path: Path, detector: ModuleType
) -> None:
    path = _write(
        tmp_path,
        "src/omnimarket/nodes/node_example/handler.py",
        'from openai import OpenAI\nclient = OpenAI(base_url="https://api.openai.com/v1")\n',
    )

    findings = detector.scan_paths(tmp_path, [path])

    assert {finding.rule_id for finding in findings} >= {
        "direct_provider_construction",
        "endpoint_invention",
    }
    assert all(finding.category == "blocking_runtime_violation" for finding in findings)
    assert any(finding.severity == "BLOCKING_RUNTIME" for finding in findings)
    assert any(finding.confidence == "HIGH_CONFIDENCE" for finding in findings)


def test_allowed_categories_distinguish_catalog_history_fixture_alias_and_generated(
    tmp_path: Path, detector: ModuleType
) -> None:
    files = [
        _write(
            tmp_path, "src/omnimarket/provider_catalog.py", 'MODEL = "qwen3-coder"\n'
        ),
        _write(tmp_path, "docs/audits/old.md", "previous model: claude-sonnet\n"),
        _write(tmp_path, "tests/fixtures/model_fixture.py", 'MODEL = "deepseek-r1"\n'),
        _write(tmp_path, "tests/test_alias_compat.py", 'MODEL = "gpt-4.1"\n'),
        _write(
            tmp_path,
            "docs/evidence/generated_runtime_artifact.json",
            '{"model":"qwen"}\n',
        ),
    ]

    categories = {
        finding.path: finding.category
        for finding in detector.scan_paths(tmp_path, files)
        if finding.rule_id == "hardcoded_model_identity"
    }

    assert (
        categories["src/omnimarket/provider_catalog.py"] == "allowed_provider_catalog"
    )
    assert categories["docs/audits/old.md"] == "allowed_historical_evidence"
    assert categories["tests/fixtures/model_fixture.py"] == "allowed_migration_fixture"
    assert (
        categories["tests/test_alias_compat.py"] == "allowed_compatibility_alias_test"
    )
    assert (
        categories["docs/evidence/generated_runtime_artifact.json"]
        == "generated_runtime_artifact"
    )


def test_escape_hatch_requires_reason_ticket_owner_and_can_recategorize(
    tmp_path: Path, detector: ModuleType
) -> None:
    marker = "# " + "model-routing-ok"
    valid = _write(
        tmp_path,
        "src/omnimarket/nodes/node_example/catalog.py",
        (
            f'MODEL = "qwen3-coder"  {marker} '
            'reason="provider catalog sample" ticket=OMN-11944 owner=runtime '
            "category=allowed_provider_catalog review_by=2026-06-30\n"
        ),
    )
    invalid = _write(
        tmp_path,
        "src/omnimarket/nodes/node_example/bad.py",
        f'MODEL = "qwen3-coder"  {marker} reason="missing owner"\n',
    )

    valid_findings = detector.scan_paths(tmp_path, [valid])
    invalid_findings = detector.scan_paths(tmp_path, [invalid])

    assert valid_findings
    assert all(finding.escaped for finding in valid_findings)
    assert {finding.category for finding in valid_findings} == {
        "allowed_provider_catalog"
    }
    assert any(
        finding.escape_hatch["ticket"] == "OMN-11944" for finding in valid_findings
    )
    assert any(
        finding.rule_id == "invalid_escape_hatch" for finding in invalid_findings
    )
    assert any(finding.severity == "BLOCKING_RUNTIME" for finding in invalid_findings)


def test_semantic_routing_path_checks_cover_fallback_retry_and_policy_bypass(
    tmp_path: Path, detector: ModuleType
) -> None:
    path = _write(
        tmp_path,
        "src/omnimarket/nodes/node_example/router.py",
        "\n".join(
            [
                'model = os.getenv("LLM_MODEL_NAME", "claude-sonnet")',
                'endpoint = os.environ.get("LLM_ENDPOINT_URL") or "https://api.openai.com/v1"',
                'max_retries = os.getenv("LLM_MAX_RETRIES", "3")',
                'if provider == "openai":',
                "    pass",
                "",
            ]
        ),
    )

    findings = detector.scan_paths(tmp_path, [path])
    rules = {finding.rule_id for finding in findings}

    assert "undeclared_routing_fallback" in rules
    assert "endpoint_invention" in rules
    assert "hidden_retry_logic" in rules
    assert "policy_bypass" in rules


def test_report_mode_cli_exits_zero_and_blocking_mode_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], detector: ModuleType
) -> None:
    _write(
        tmp_path,
        "src/omnimarket/nodes/node_example/handler.py",
        'from openai import OpenAI\nclient = OpenAI(model="gpt-4.1")\n',
    )

    report_exit = detector.main(
        ["--root", str(tmp_path), "--mode", "report", "--format", "json"]
    )
    report_payload = json.loads(capsys.readouterr().out)
    blocking_exit = detector.main(["--root", str(tmp_path), "--mode", "blocking"])

    assert report_exit == 0
    assert report_payload["blocking_count"] >= 1
    assert blocking_exit == 1
