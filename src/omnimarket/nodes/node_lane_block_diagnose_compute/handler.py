# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure ownership diagnosis from named file and package evidence, never prose inference."""

from __future__ import annotations

import re

from omnimarket.models.model_lane_routing import (
    ModelLaneBlockFacts,
    ModelOwnerDiagnosis,
)

# Paths in traceback quotes, markdown backticks and plain report text, with optional line suffixes.
_PATH = re.compile(r"(?<![\w.-])(?:[A-Za-z]:)?/?(?:[\w.-]+/)+[\w.-]+")


def named_paths(text: str) -> tuple[str, ...]:
    """Keep first mention order; paths only, without a traceback's line-number suffix."""
    return tuple(dict.fromkeys(match.group(0) for match in _PATH.finditer(text)))


def path_matches(path: str, prefix: str) -> bool:
    """An overlay prefix matches complete path components, including absolute checkouts."""
    prefix = prefix.strip("/")
    if not prefix or ".." in prefix.split("/") or ".." in path.split("/"):
        return False
    return re.search(r"(?:^|/)" + re.escape(prefix) + r"(?:/|$)", path) is not None


class HandlerLaneBlockDiagnoseCompute:
    def handle(self, request: ModelLaneBlockFacts) -> ModelOwnerDiagnosis:
        evidence: dict[str, str] = {}
        # Files have been read by the effect. Their contents can name an owning package;
        # their paths are themselves explicit evidence, without interpreting prose.
        sources = [request.blocker, request.report_text]
        sources.extend(f"{file.path}\n{file.text}" for file in request.files)
        for source in sources:
            for line in source.splitlines():
                paths = named_paths(line)
                for rule in request.owner_map.rules:
                    path_hit = any(
                        path_matches(path, prefix)
                        for path in paths
                        for prefix in rule.paths
                    )
                    package_hit = any(
                        re.search(
                            r"(?<![\w.-])" + re.escape(package) + r"(?![\w-])", line
                        )
                        for package in rule.packages
                        if package
                    )
                    if path_hit or package_hit:
                        evidence.setdefault(rule.repository, line)
        if len(evidence) == 1:
            owner, line = next(iter(evidence.items()))
            return ModelOwnerDiagnosis(
                owner=owner,
                confidence=1,
                evidence_line=line,
                reason="named file or package maps to one repository",
            )
        return ModelOwnerDiagnosis(
            reason=(
                "conflicting named owners: " + ", ".join(sorted(evidence))
                if evidence
                else "no named file or package in the owner map"
            )
        )
