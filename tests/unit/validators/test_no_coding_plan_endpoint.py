# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20173: falsifiable config gate, including its structural exception."""

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.validators.no_coding_plan_endpoint import main

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
CODING_URL = "https://api.z.ai/api/coding/paas/v4"
CATALOGUE = "src/omnimarket/configs/byok_provider_backends.v1.yaml"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return tmp_path


def track(tree: Path, name: str, content: str) -> None:
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(
        ["git", "-C", str(tree), "add", "--", name],
        check=True,
        env=scrub_git_location_env(os.environ),
    )


def test_clean_tree_passes(tree: Path) -> None:
    track(tree, "contract.yaml", "endpoint: https://api.z.ai/api/paas/v4\n")
    assert main(["--root", str(tree)]) == 0


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (
            "docker-compose.yml",
            f"services:\n  app:\n    environment:\n      LLM_GLM_URL: ${{LLM_GLM_URL:-{CODING_URL}}}\n",
        ),
        ("contract.yaml", f'endpoint: "{CODING_URL}"\n'),
        ("contract.yaml", "endpoint: https://other.example/api/coding\n"),
        ("contract.yaml", f"description: >-\n  Must never address {CODING_URL}\n"),
        ("settings.json", f'{{"endpoint": "{CODING_URL}"}}\n'),
        ("settings.toml", f'endpoint = "{CODING_URL}"\n'),
        (".env", f"LLM_GLM_URL={CODING_URL}\n"),
        (".env.local", f"LLM_GLM_URL={CODING_URL}\n"),
        ("settings.env.template", f"LLM_GLM_URL={CODING_URL}\n"),
        ("docker-compose", f"LLM_GLM_URL={CODING_URL}\n"),
    ],
)
def test_endpoint_fails(
    tree: Path, name: str, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    track(tree, name, content)
    assert main(["--root", str(tree)]) == 1
    assert name in capsys.readouterr().err


def test_comments_and_hostless_paths_pass(tree: Path) -> None:
    track(
        tree,
        "contract.yaml",
        f'# endpoint: {CODING_URL}\npath: "/api/coding/paas/v4" # {CODING_URL}\n',
    )
    track(
        tree,
        ".env.template",
        f"# {CODING_URL}\nENDPOINT=https://api.z.ai/api/paas/v4\n",
    )
    assert main(["--root", str(tree)]) == 0


@pytest.mark.parametrize("flag", ["false", "true", '"false"', "null"])
def test_detection_exception_requires_false_flag(tree: Path, flag: str) -> None:
    track(
        tree,
        CATALOGUE,
        f'providers:\n  - provider: glm\n    customer_routable: {flag}\n    endpoint_url: "{CODING_URL}"\n',
    )
    assert main(["--root", str(tree)]) == (0 if flag == "false" else 1)


def test_false_flag_elsewhere_is_not_an_exception(tree: Path) -> None:
    track(
        tree, "contract.yaml", f'customer_routable: false\nendpoint: "{CODING_URL}"\n'
    )
    assert main(["--root", str(tree)]) == 1


def test_false_flag_does_not_exempt_sibling_rows(tree: Path) -> None:
    track(
        tree,
        CATALOGUE,
        f'providers:\n  - customer_routable: false\n    endpoint_url: "{CODING_URL}"\n  - endpoint_url: "{CODING_URL}"\n',
    )
    assert main(["--root", str(tree)]) == 1


def test_only_tracked_config_is_scanned(tree: Path) -> None:
    track(tree, "tests/fixture.yaml", f'endpoint: "{CODING_URL}"\n')
    track(tree, "docs/example.yaml", f'endpoint: "{CODING_URL}"\n')
    track(tree, "README.md", CODING_URL)
    (tree / "untracked.yaml").write_text(f'endpoint: "{CODING_URL}"\n')
    assert main(["--root", str(tree)]) == 0


def test_invalid_yaml_fails_closed(tree: Path) -> None:
    track(tree, "contract.yaml", "endpoint: [\n")
    assert main(["--root", str(tree)]) == 1


def test_real_repo_passes() -> None:
    assert main(["--root", str(ROOT)]) == 0
