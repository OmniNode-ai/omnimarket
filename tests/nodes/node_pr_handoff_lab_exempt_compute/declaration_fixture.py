# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host_install declaration as the deployment overlay holds it after OMN-19998, for the install-first tests.

``BASE_EXCLUDED`` is the excluded_paths list before the two CI-only folders joined it; ``CI_ONLY`` is what the change adds.
"""

from __future__ import annotations

from typing import Any

BASE_EXCLUDED = [
    "tests/*",
    "*/tests/*",
    "docs/*",
    "*/docs/*",
    "README.md",
    "*/README.md",
    "CHANGELOG.md",
    "*/CHANGELOG.md",
    "contracts/*",
    "*.md.tmpl",
    "scripts/application-relation-ownership.yaml",
    "*/scripts/application-relation-ownership.yaml",
]
CI_ONLY = [
    "scripts/ci/*",
    "*/scripts/ci/*",
    "scripts/validation/*",
    "*/scripts/validation/*",
]


def declaration(excluded: list[str] | None = None) -> dict[str, Any]:
    return {
        "paths": [
            "launchd/*",
            "*/launchd/*",
            "systemd/*",
            "*/systemd/*",
            "*.plist",
            "*.plist.tmpl",
            "*.service",
            "*.service.tmpl",
            "*.timer",
            "*.timer.tmpl",
            "hooks/*",
            "*/hooks/*",
            "plugins/*",
            ".claude-plugin/*",
            "*/.claude-plugin/*",
            "scripts/*",
            "*/scripts/*",
            "bin/*",
            "*/bin/*",
        ],
        "repo_paths": {
            "example-repo": ["src/*"],
            "other-repo": ["src/*", "config/*"],
        },
        "excluded_paths": BASE_EXCLUDED + CI_ONLY if excluded is None else excluded,
        "prod_paths": [
            "grants/*",
            "*/grants/*",
            "prod/*",
            "*/prod/*",
            "*prod_promotion*",
            "*/manifests/prod*",
        ],
        "installed_cell": "installed",
        "observed_cell": "observed",
        "observed_words": [
            "tick",
            "fired",
            "firing",
            "ran",
            "running",
            "loaded",
            "invoked",
            "called",
            "launchctl",
            "systemctl",
            "exit 0",
            "exit=0",
            "returned",
            "printed",
            "output",
            "started",
        ],
        "observed_refused": [
            r"^\W*(\d+\s+)?(focused\s+|unit\s+)?(tests?|pytest|ci)\b[^.;]*\b(pass|passed|passes|green|ok)\W*$"
        ],
        "min_observed_chars": 12,
        "ruling": "synthetic fixture of the deployment document",
    }
