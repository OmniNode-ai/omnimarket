# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics the contracts declare: from given mappings (the in-memory default) or the contract
files under the registry root the overlay names.

The file backend walks every contract file the overlay's glob finds in the overlay's repositories,
skipping the path parts the overlay excludes, and records, for every string that fully matches the
overlay's topic pattern, the contract that holds it. A contract declares a topic as published when
some key on the path to the string carries one of the overlay's publish fragments.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    ModelContractSource,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    DeclaredTopicsError,
)


class InMemoryDeclaredTopics:
    """The contracts say given topic mappings; without a producer map the declared map is it."""

    def __init__(
        self,
        declared: Mapping[str, tuple[str, ...]],
        producers: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._declared = {
            topic: tuple(sorted(names)) for topic, names in declared.items()
        }
        self._producers = (
            {topic: tuple(sorted(names)) for topic, names in producers.items()}
            if producers is not None
            else self._declared
        )

    def declared(self) -> dict[str, tuple[str, ...]]:
        return self._declared

    def producers(self) -> dict[str, tuple[str, ...]]:
        return self._producers


class ContractFileDeclaredTopics:
    """The topics the contract files under the registry root declare, by topic and contract."""

    def __init__(self, source: ModelContractSource, environ: Mapping[str, str]) -> None:
        self._source = source
        self._environ = environ
        self._declared: dict[str, set[str]] = {}
        self._publish: dict[str, set[str]] = {}
        self._read()

    def declared(self) -> dict[str, tuple[str, ...]]:
        return {topic: tuple(sorted(names)) for topic, names in self._declared.items()}

    def producers(self) -> dict[str, tuple[str, ...]]:
        return {topic: tuple(sorted(names)) for topic, names in self._publish.items()}

    def _files(self) -> list[Path]:
        source = self._source
        root = self._environ.get(source.registry_root_env, "").strip()
        if not root:
            raise DeclaredTopicsError("registry-root-variable-unset")
        files: list[Path] = []
        for repo in source.repositories:
            base = Path(root) / repo
            if not base.is_dir():
                continue
            for path in sorted(base.glob(source.contract_glob)):
                if not path.is_file():
                    continue
                if any(part in source.excluded_parts for part in path.parts):
                    continue
                files.append(path)
        if not files:
            raise DeclaredTopicsError("no-contract-files")
        return files

    def _read(self) -> None:
        source = self._source
        pattern = re.compile(source.topic_pattern)
        parsed = 0
        for path in self._files():
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError, OSError):
                continue
            parsed += 1
            self._walk(document, pattern, path)
        if parsed == 0:
            raise DeclaredTopicsError("no-contract-parsed")

    def _walk(self, document: object, pattern: re.Pattern[str], path: Path) -> None:
        source = self._source
        name = (
            document["name"]
            if isinstance(document, dict) and isinstance(document.get("name"), str)
            else path.parent.name
        )

        def visit(node: object, keys: list[str]) -> None:
            if isinstance(node, str):
                if pattern.fullmatch(node):
                    self._declared.setdefault(node, set()).add(name)
                    if any(
                        fragment in key
                        for key in keys
                        for fragment in source.publish_key_fragments
                    ):
                        self._publish.setdefault(node, set()).add(name)
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    visit(value, [*keys, str(key)])
            elif isinstance(node, (list, tuple)):
                for value in node:
                    visit(value, keys)

        visit(document, [])


__all__ = ["ContractFileDeclaredTopics", "InMemoryDeclaredTopics"]
