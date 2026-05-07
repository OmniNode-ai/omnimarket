# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enforce that .env.example stays in sync with the Settings model.

OMN-10581. Wave 1 / Task 9 of the Public-Shippable plan.

Runs generate_env_example.py and diffs the output against the committed
.env.example. Fails if they differ, ensuring contributors regenerate after
adding or removing Settings fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_EXAMPLE_PATH = _REPO_ROOT / ".env.example"
_GENERATE_SCRIPT = _REPO_ROOT / "scripts" / "generate_env_example.py"


@pytest.mark.unit
def test_env_example_is_in_sync_with_settings() -> None:
    """The committed .env.example must match what generate_env_example.py produces.

    Re-run `uv run python scripts/generate_env_example.py` if this fails.
    """
    assert _GENERATE_SCRIPT.exists(), f"Generator script not found: {_GENERATE_SCRIPT}"
    assert _ENV_EXAMPLE_PATH.exists(), (
        f".env.example not found at {_ENV_EXAMPLE_PATH}. "
        "Run: uv run python scripts/generate_env_example.py"
    )

    # Import the generator module directly so we don't spawn a subprocess.
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

    # Load via importlib to avoid name collision with 'generate_env_example' if
    # it were ever added as a package.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "generate_env_example", _GENERATE_SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    generated = mod.generate(output_path=None)
    committed = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert generated == committed, (
        ".env.example is out of sync with the Settings model.\n"
        "Re-generate it by running:\n\n"
        "    uv run python scripts/generate_env_example.py\n\n"
        "Then commit the updated .env.example."
    )
