# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the repo-grounded SWE smoke corpus (OMN-13988)."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.delegation.swe_discriminator.models import SweTask

DEFAULT_CORPUS_PATH = Path(__file__).resolve().parent / "smoke_corpus.yaml"


def load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> list[SweTask]:
    raw = yaml.safe_load(path.read_text())
    return [SweTask(**item) for item in raw["tasks"]]
