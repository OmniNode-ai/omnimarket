# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the runtime sweep CI helper."""

from __future__ import annotations

import pytest

from scripts.ci import run_runtime_sweep


@pytest.mark.unit
def test_import_model_ref_skips_symbolic_model_names() -> None:
    run_runtime_sweep._import_model_ref("ModelCanaryCommandPayload")


@pytest.mark.unit
def test_import_model_ref_imports_structured_module_name() -> None:
    run_runtime_sweep._import_model_ref(
        {
            "module": "scripts.ci.run_runtime_sweep",
            "name": "LIFECYCLE_EXEMPTIONS",
        }
    )


@pytest.mark.unit
def test_import_model_ref_rejects_missing_structured_ref() -> None:
    with pytest.raises(AttributeError):
        run_runtime_sweep._import_model_ref(
            {
                "module": "scripts.ci.run_runtime_sweep",
                "name": "MissingModel",
            }
        )
