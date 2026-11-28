# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A work-contract pin on uv.lock survives the post-release version bump (OMN-26733).

The post-release bump rewrites the project's own version line in uv.lock (and the
lock's format revision), which broke every contract that pinned the whole file by
hash. The pin now hashes the lock with those two lines normalized, so the bump
passes and any real dependency change still fails.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from omnimarket.handlers.work_contracts.loader import pinned_path
from omnimarket.nodes.node_model_work_contract_compute.models.model_work_contract import (
    InputPin,
)

pytestmark = pytest.mark.unit

LOCK = (
    'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
    '[[package]]\nname = "aiokafka"\nversion = "0.12.0"\nsource = { registry = "x" }\n\n'
    '[[package]]\nname = "omnimarket"\nversion = "{v}"\nsource = { editable = "." }\n'
)


def _normalized_digest(text: str) -> str:
    base = text.replace("{v}", "0.0.0").replace("revision = 3\n", "revision = 0\n")
    return sha256(base.encode()).hexdigest()


def _pin(root: Path) -> InputPin:
    return InputPin(path="uv.lock", sha256=_pin_digest())


def _pin_digest() -> str:
    return _normalized_digest(LOCK)


def test_bumped_version_and_revision_still_match_the_pin(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(
        LOCK.replace("{v}", "0.4.331").replace("revision = 3", "revision = 5")
    )
    assert pinned_path(tmp_path, _pin(tmp_path)) == (tmp_path / "uv.lock").resolve()


def test_known_bad_dependency_change_still_fails(tmp_path: Path) -> None:
    """Positive control: a real dependency edit is not normalized away."""
    (tmp_path / "uv.lock").write_text(
        LOCK.replace("{v}", "0.4.331").replace("0.12.0", "0.13.0")
    )
    with pytest.raises(ValueError, match=r"input_pin_mismatch: uv\.lock"):
        pinned_path(tmp_path, _pin(tmp_path))
