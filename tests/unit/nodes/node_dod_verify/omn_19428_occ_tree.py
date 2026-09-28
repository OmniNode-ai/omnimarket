# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19428 -- an `onex_change_control` tree for a test contract to live in.

The collector reads every evidence item field the core model does not own from
`onex_change_control`'s `ModelDodEvidenceItem`, in the OCC tree the contract was
read from. A test contract that carries such a field therefore has to live in
an OCC tree, exactly as a real one does. By default that tree holds the REAL
model file, verbatim at a pinned OCC commit (the fixture below); a test that
needs a different field list passes its own model source.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "occ"

#: Where `onex_change_control` keeps the model, relative to its root.
MODEL_RELPATH = Path("src/onex_change_control/models/model_dod_check.py")

#: `onex_change_control` at commit 3d1bbb66693ca5c66dc09b8a7f3d3816960f1d15,
#: blob ae576b07744d756ae3c279b9f7a6bceefb225bff, byte for byte.
REAL_MODEL_FIXTURE = FIXTURES / "omn_19428_occ_model_dod_check.py.txt"


def occ_tree(tmp_path: Path, model_source: str | None = None) -> Path:
    """Create ``<tmp_path>/onex_change_control`` holding a model and ``contracts/``."""
    root = tmp_path / "onex_change_control"
    model = root / MODEL_RELPATH
    model.parent.mkdir(parents=True)
    if model_source is None:
        shutil.copyfile(REAL_MODEL_FIXTURE, model)
    else:
        model.write_text(model_source, encoding="utf-8")
    (root / "contracts").mkdir()
    return root


def occ_contract(
    tmp_path: Path,
    items: list[dict[str, object]],
    *,
    model_source: str | None = None,
    ticket_id: str = "OMN-9999",
) -> str:
    """Write a contract into a fresh OCC tree and return its path."""
    root = occ_tree(tmp_path, model_source)
    path = root / "contracts" / f"{ticket_id}.yaml"
    path.write_text(
        yaml.safe_dump({"ticket_id": ticket_id, "dod_evidence": items}),
        encoding="utf-8",
    )
    return str(path)
