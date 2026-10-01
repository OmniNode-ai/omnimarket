# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20173: refuse Coding Plan endpoint addresses in tracked config.

YAML string values (including prose values) are scanned after parsing, never
comments or mapping keys. Other config files are scanned by non-comment line.
Only detection-only provider rows in the BYOK catalogue are exempt, and only
when their customer_routable flag is the YAML boolean false.

Run: uv run python -m omnimarket.validators.no_coding_plan_endpoint
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

_ENDPOINT = re.compile(
    r"""https?://[^\s"']*/api/coding(/|$)|api\.z\.ai/api/coding""",
    re.IGNORECASE,
)
_CATALOGUE = Path("src/omnimarket/configs/byok_provider_backends.v1.yaml")
_ROOT = Path(__file__).resolve().parents[3]
_STRING_TAG = "tag:yaml.org,2002:str"
_BOOL_TAG = "tag:yaml.org,2002:bool"


def _config_file(path: Path) -> bool:
    if any(part in {"tests", "docs"} for part in path.parts):
        return False
    name = path.name.lower()
    return (
        path.suffix.lower() in {".yaml", ".yml", ".json", ".toml", ".env", ".template"}
        or name == ".env"
        or ".env." in name
        or name.startswith(("docker-compose", "docker.compose", "compose."))
        or name == "compose"
    ) and path.suffix.lower() != ".md"


def _detection_only(node: MappingNode) -> bool:
    return any(
        isinstance(key, ScalarNode)
        and key.value == "customer_routable"
        and isinstance(value, ScalarNode)
        and value.tag == _BOOL_TAG
        and value.value.lower() == "false"
        for key, value in node.value
    )


def _yaml_findings(node: Node, path: Path, key_path: str = "$") -> list[str]:
    if isinstance(node, ScalarNode):
        if node.tag == _STRING_TAG and _ENDPOINT.search(node.value):
            return [
                f"{path}:{node.start_mark.line + 1} ({key_path}): Coding Plan endpoint"
            ]
        return []
    findings: list[str] = []
    if isinstance(node, MappingNode):
        # The exemption is row-local: a false flag never exempts siblings or
        # an arbitrary contract. Changing it to true immediately closes it.
        if (
            path == _CATALOGUE
            and re.fullmatch(r"\$\.providers\[\d+\]", key_path)
            and _detection_only(node)
        ):
            return []
        for key, value in node.value:
            label = key.value if isinstance(key, ScalarNode) else "<mapping>"
            findings.extend(_yaml_findings(value, path, f"{key_path}.{label}"))
    elif isinstance(node, SequenceNode):
        for index, value in enumerate(node.value):
            findings.extend(_yaml_findings(value, path, f"{key_path}[{index}]"))
    return findings


def find_coding_plan_endpoints(root: Path) -> list[str]:
    """Scan tracked working-tree config; git and parse failures fail closed."""
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    findings: list[str] = []
    for filename in tracked.split("\0"):
        if not filename:
            continue
        relative = Path(filename)
        absolute = root / relative
        if not _config_file(relative) or not absolute.is_file():
            continue
        content = absolute.read_text(encoding="utf-8")
        if relative.suffix.lower() in {".yaml", ".yml"}:
            try:
                for node in yaml.compose_all(content, Loader=yaml.SafeLoader):
                    if node is not None:
                        findings.extend(_yaml_findings(node, relative))
            except yaml.YAMLError as exc:
                findings.append(f"{relative}: invalid YAML: {exc}")
        else:
            for line_number, line in enumerate(content.splitlines(), start=1):
                if line.lstrip().startswith(("#", "//", ";")):
                    continue
                if _ENDPOINT.search(line):
                    findings.append(f"{relative}:{line_number}: Coding Plan endpoint")
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """CLI shared by CI, pre-commit and the injection tests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_ROOT)
    args = parser.parse_args(argv)
    try:
        findings = find_coding_plan_endpoints(args.root.resolve())
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"Cannot scan tracked config: {exc}\n")
        return 1
    for finding in findings:
        sys.stderr.write(f"{finding}\n")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
