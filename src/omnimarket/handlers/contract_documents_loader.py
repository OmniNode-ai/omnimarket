# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Adapter for loading contract documents from YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.models.model_contract_topics import ModelContractDocument


def load_contract_documents(root: Path) -> tuple[ModelContractDocument, ...]:
    """Load all contract documents from YAML files under root.

    Args:
        root: The root directory to search for contract directories.

    Returns:
        A tuple of ModelContractDocument instances.
    """
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    results: list[ModelContractDocument] = []

    for path in sorted(root.glob("*/contract.yaml")):
        directory_name = path.parent.name
        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.load(f, Loader=loader)
        except (yaml.YAMLError, UnicodeDecodeError, OSError) as e:
            results.append(
                ModelContractDocument(
                    name=directory_name,
                    path=str(path),
                    read_error=str(e),
                )
            )
            continue

        if not isinstance(data, dict):
            results.append(
                ModelContractDocument(
                    name=directory_name,
                    path=str(path),
                    read_error="contract is not a mapping",
                )
            )
            continue

        name = data.get("name")
        if not isinstance(name, str) or not name:
            name = directory_name

        results.append(
            ModelContractDocument(
                name=name,
                path=str(path),
                document=data,
            )
        )

    return tuple(results)
