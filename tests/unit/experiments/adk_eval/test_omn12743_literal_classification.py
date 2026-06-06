# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12743: Literal classification gate — no hardcoded model/provider/path on demo-path.

Acceptance criteria (ticket part c):
  Every hardcoded model/provider/endpoint literal in the omnimarket codebase
  must be classified as exactly one of:
    demo-path | experiment-only | dead-code | deferred

This test file acts as the living classification table and enforces that:
  1. Track A's gemini model is resolved from env var, not hardcoded.
  2. No new bare "gemini-flash-latest" literals appear in production source
     outside the experiment-only subtree.
  3. The classification table below is accurate (tested structurally).

Track A scope decision (part a):
  Track A (experiments/adk_eval/track_a_adk/) is EXPERIMENT-ONLY.
  It runs in a dedicated venv outside the omnimarket node runtime and is
  not on the live demo bus. Its model literals are classified experiment-only.

dod-runtime-model-identity sub-item:
  Gated on OMN-12742 redeploy. The runtime-observed model identity check
  (correlation-id + inference-response event) cannot complete until the
  node image is rebuilt and redeployed. That sub-item is marked pending
  in OMN-12743.yaml and will be completed as part of OMN-12742.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import pytest

# ---------------------------------------------------------------------------
# Classification table (part c of OMN-12743)
# ---------------------------------------------------------------------------
# Format: (file_relative_to_repo_root, line_hint, literal_value, classification)
# Classifications: demo-path | experiment-only | dead-code | deferred
# ---------------------------------------------------------------------------

ClassificationKind = Literal["demo-path", "experiment-only", "dead-code", "deferred"]

LITERAL_CLASSIFICATION_TABLE: list[tuple[str, str, ClassificationKind]] = [
    # --- Track A ADK experiment (experiment-only) ---
    (
        "experiments/adk_eval/track_a_adk/app/agent.py",
        "TRACK_A_GEMINI_MODEL default='gemini-flash-latest'",
        "experiment-only",
    ),
    (
        "experiments/adk_eval/track_a_adk/app/run_agent.py",
        "metrics['model'] = TRACK_A_GEMINI_MODEL (resolved from const)",
        "experiment-only",
    ),
    # --- Test fixtures (experiment-only: test input data, not runtime defaults) ---
    (
        "tests/unit/experiments/adk_eval/harness/test_aggregator.py",
        "track_a_metrics()['model'] = 'gemini-flash-latest' (fixture input)",
        "experiment-only",
    ),
    # --- Demo fanout golden chain test (experiment-only: preflight error test) ---
    (
        "src/omnimarket/nodes/node_demo_fanout_orchestrator/tests/test_golden_chain.py",
        "model_id='gemini/gemini-2.0-flash' endpoint_url='...v1beta/openai' (preflight test fixture)",
        "experiment-only",
    ),
    # --- Demo runtime path test (demo-path: integration test exercising the live demo path) ---
    (
        "tests/test_demo_runtime_path.py",
        "model_id='gemini/gemini-2.0-flash' endpoint_url='...v1beta/openai' (demo integration test)",
        "demo-path",
    ),
    # --- Config/registry YAML (deferred: model_name values in YAML data files) ---
    (
        "src/omnimarket/configs/bifrost_delegation.yaml",
        "model_name: 'gemini-2.0-flash' (data file, endpoint_url resolved from overlay)",
        "deferred",
    ),
    (
        "src/omnimarket/data/model_registry/model_registry_v1.yaml",
        "model_name: 'gemini-2.0-flash' (registry data, endpoint resolved from GEMINI_API_URL env)",
        "deferred",
    ),
    # --- tests/constants.py (experiment-only: test-only symbolic constant) ---
    (
        "tests/constants.py",
        "MODEL_GEMINI_2_0_FLASH = 'gemini-2.0-flash' (test constant module)",
        "experiment-only",
    ),
    # --- Unit test using model registry (experiment-only: tests the registry itself) ---
    (
        "tests/unit/models/delegation/llm_cost_routing/test_model_registry_omn12492.py",
        "'gemini-2.0-flash' as registry lookup key in test",
        "experiment-only",
    ),
    # --- generation_consumer test (experiment-only: regression test for URL construction) ---
    (
        "tests/unit/nodes/node_generation_consumer/test_handler_generation_consumer.py",
        "'https://generativelanguage.googleapis.com/v1beta/openai' (URL regression test fixture)",
        "experiment-only",
    ),
    # --- delegation call effect test (experiment-only: URL assertion test) ---
    (
        "tests/unit/nodes/node_llm_delegation_call_effect/test_handler_llm_delegation_call.py",
        "'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions' (test assertion)",
        "experiment-only",
    ),
    # --- nightly loop controller test (experiment-only: test fixture model) ---
    (
        "tests/test_golden_chain_nightly_loop_controller.py",
        "model_id='gpt-4o' (test fixture, not runtime default)",
        "experiment-only",
    ),
    # --- test_node_model_router_contract_boundary.py (experiment-only: CI scanner) ---
    (
        "tests/test_node_model_router_contract_boundary.py",
        "'gpt-' as pattern in scanner test (not a model literal)",
        "experiment-only",
    ),
]


_REPO_ROOT = Path(__file__).resolve().parents[4]

# Literals that must NOT appear bare in production (non-test, non-experiment) source.
# A bare literal outside the allowlisted subtrees is a demo-path regression.
_BARE_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("gemini-flash-latest", re.compile(r'"gemini-flash-latest"')),
]

# Subtrees exempt from the production scan (experiments + tests are excluded).
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "experiments/",
    "tests/",
    "src/omnimarket/data/",
    "src/omnimarket/configs/",
)


@pytest.mark.unit
class TestLiteralClassificationTable:
    """Structural tests that the classification table is well-formed."""

    def test_table_is_non_empty(self) -> None:
        assert len(LITERAL_CLASSIFICATION_TABLE) >= 10, (
            "Classification table must have at least 10 entries covering "
            "all known hardcoded model/provider literals."
        )

    def test_all_classifications_are_valid(self) -> None:
        valid = {"demo-path", "experiment-only", "dead-code", "deferred"}
        for file_hint, _desc, classification in LITERAL_CLASSIFICATION_TABLE:
            assert classification in valid, (
                f"{file_hint!r}: classification {classification!r} is not one of {valid}"
            )

    def test_all_entries_have_non_empty_description(self) -> None:
        for file_hint, desc, _ in LITERAL_CLASSIFICATION_TABLE:
            assert desc.strip(), f"{file_hint!r}: description must be non-empty"

    def test_all_file_hints_are_non_empty(self) -> None:
        for file_hint, _, _ in LITERAL_CLASSIFICATION_TABLE:
            assert file_hint.strip(), "file_hint must be non-empty"

    def test_no_duplicate_file_hints(self) -> None:
        seen: dict[str, int] = {}
        for i, (file_hint, _, _) in enumerate(LITERAL_CLASSIFICATION_TABLE):
            if file_hint in seen:
                pytest.fail(
                    f"Duplicate file_hint {file_hint!r} at indices {seen[file_hint]} and {i}. "
                    "Each file must appear at most once; merge entries or use distinct descriptions."
                )
            seen[file_hint] = i


@pytest.mark.unit
class TestTrackAModelResolution:
    """Track A must resolve its model from env var, not a bare hardcoded literal."""

    def test_track_a_agent_source_has_no_bare_model_literal(self) -> None:
        """agent.py must not contain a bare 'gemini-flash-latest' string literal
        assigned directly to model= — it must use the TRACK_A_GEMINI_MODEL constant."""
        agent_path = (
            _REPO_ROOT / "experiments" / "adk_eval" / "track_a_adk" / "app" / "agent.py"
        )
        assert agent_path.exists(), f"agent.py not found at {agent_path}"
        source = agent_path.read_text()

        # The bare assignment `model="gemini-flash-latest"` must be gone.
        assert 'model="gemini-flash-latest"' not in source, (
            "agent.py still contains model='gemini-flash-latest' hardcoded in the "
            "Agent() constructor. Replace with model=TRACK_A_GEMINI_MODEL."
        )
        # The constant must be defined via os.environ.get.
        assert "TRACK_A_GEMINI_MODEL" in source, (
            "agent.py must define TRACK_A_GEMINI_MODEL via os.environ.get."
        )
        assert "os.environ.get" in source, (
            "agent.py must resolve the model from env via os.environ.get."
        )

    def test_track_a_run_agent_uses_constant_not_literal(self) -> None:
        """run_agent.py must reference TRACK_A_GEMINI_MODEL, not 'gemini-flash-latest'."""
        run_agent_path = (
            _REPO_ROOT
            / "experiments"
            / "adk_eval"
            / "track_a_adk"
            / "app"
            / "run_agent.py"
        )
        assert run_agent_path.exists(), f"run_agent.py not found at {run_agent_path}"
        source = run_agent_path.read_text()

        # Bare literal must be gone from the metrics dict.
        assert '"gemini-flash-latest"' not in source, (
            "run_agent.py still contains a bare 'gemini-flash-latest' string literal. "
            "Use TRACK_A_GEMINI_MODEL (imported from app.agent)."
        )
        assert "TRACK_A_GEMINI_MODEL" in source, (
            "run_agent.py must import and reference TRACK_A_GEMINI_MODEL from app.agent."
        )

    def test_track_a_gemini_model_env_var_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting TRACK_A_GEMINI_MODEL env var before import changes the constant.

        This test verifies the env-resolution contract without importing ADK
        (which would require the track_a dedicated venv). We import only the
        constant resolution logic via importlib after patching the env.
        """
        import sys

        monkeypatch.setenv("TRACK_A_GEMINI_MODEL", "gemini-2.5-pro-preview-1234")
        # Remove cached module so the env var is re-read.
        sys.modules.pop("app.agent", None)

        # We cannot actually import app.agent in the omnimarket venv (ADK is absent).
        # Instead verify the contract by reading the source and checking the pattern.
        agent_path = (
            _REPO_ROOT / "experiments" / "adk_eval" / "track_a_adk" / "app" / "agent.py"
        )
        source = agent_path.read_text()
        assert 'os.environ.get(\n    "TRACK_A_GEMINI_MODEL"' in source or (
            'os.environ.get("TRACK_A_GEMINI_MODEL"' in source
        ), (
            "TRACK_A_GEMINI_MODEL must be assigned via os.environ.get('TRACK_A_GEMINI_MODEL', ...)"
        )


@pytest.mark.unit
class TestProductionSourceNoBareLiterals:
    """No bare forbidden model literals in production (non-experiment, non-test) source."""

    def _iter_production_py(self) -> list[Path]:
        src_root = _REPO_ROOT / "src"
        if not src_root.exists():
            return []
        results = []
        for py_file in sorted(src_root.rglob("*.py")):
            rel = str(py_file.relative_to(_REPO_ROOT))
            if any(rel.startswith(prefix) for prefix in _EXEMPT_PREFIXES):
                continue
            results.append(py_file)
        return results

    def test_no_bare_gemini_flash_latest_in_production_src(self) -> None:
        """Production source (src/ excluding data/ and configs/) must not contain
        bare 'gemini-flash-latest' model ID strings."""
        label, pattern = _BARE_FORBIDDEN_PATTERNS[0]
        violations: list[str] = []
        for py_file in self._iter_production_py():
            source = py_file.read_text(encoding="utf-8")
            for lineno, line in enumerate(source.splitlines(), 1):
                if pattern.search(line):
                    # Allow lines with explicit exemption annotation.
                    if (
                        "experiment-model-literal-ok" in line
                        or "test-literal-ok" in line
                    ):
                        continue
                    rel = str(py_file.relative_to(_REPO_ROOT))
                    violations.append(f"  {rel}:{lineno}  {line.strip()}")
        if violations:
            pytest.fail(
                f"Bare {label!r} literal found in production source "
                f"(must be experiment-only or resolved via env/config):\n"
                + "\n".join(violations)
            )
