# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Dependency provenance accepts only a sibling's own dev merge tag from git."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_dep_provenance.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_dep_provenance", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod():
    return _load_module()


def _write_pyproject(tmp_path: Path, sources_block: str) -> Path:
    """Write a minimal pyproject.toml with the given [tool.uv.sources] block."""
    content = (
        "[project]\n"
        'name = "omnibase-infra"\n'
        'version = "0.0.0"\n'
        "dependencies = [\n"
        "    # Example comment that also mentions [tool.uv.sources]\n"
        '    #   omnibase-core = { git = "...", rev = "..." }\n'
        '    "omnibase-core>=0.46.1,<0.47.0",\n'
        '    "omnibase-spi>=0.23.0,<0.24.0",\n'
        "]\n"
        "\n"
        f"{sources_block}"
        "\n"
        "[tool.ruff]\n"
        'target-version = "py312"\n'
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# REJECT: reproduce #2184's L193-194 core/spi git-rev overrides
# ---------------------------------------------------------------------------


def test_reject_core_spi_git_rev_override(mod, tmp_path: Path) -> None:
    block = (
        "[tool.uv.sources]\n"
        'omnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", '
        'rev = "2a07385dec0ff06903f62572c546ec201f964aaf" }\n'
        'omnibase-spi = { git = "https://github.com/OmniNode-ai/omnibase_spi.git", '
        'rev = "cdfe1a470e96cbe8414ba6b08bbc99a452f09018" }\n'
    )
    path = _write_pyproject(tmp_path, block)

    violations = mod.find_violations(path.read_text())
    assert len(violations) == 2
    assert any("omnibase-core" in v for v in violations)
    assert any("omnibase-spi" in v for v in violations)

    assert mod.main(["--pyproject", str(path)]) == 1


def test_reject_underscore_spelling(mod, tmp_path: Path) -> None:
    """Underscore spelling (omnibase_core) is caught the same as hyphen."""
    block = (
        "[tool.uv.sources]\n"
        'omnibase_core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", '
        'rev = "deadbeef" }\n'
    )
    path = _write_pyproject(tmp_path, block)
    assert mod.main(["--pyproject", str(path)]) == 1


def test_reject_branch_override(mod, tmp_path: Path) -> None:
    block = (
        "[tool.uv.sources]\n"
        'omnibase-compat = { git = "https://github.com/OmniNode-ai/omnibase_compat.git", '
        'branch = "dev" }\n'
    )
    path = _write_pyproject(tmp_path, block)
    assert mod.main(["--pyproject", str(path)]) == 1


def test_reject_tag_override(mod, tmp_path: Path) -> None:
    block = (
        "[tool.uv.sources]\n"
        'omnibase-spi = { git = "https://github.com/OmniNode-ai/omnibase_spi.git", '
        'tag = "v0.23.0" }\n'
    )
    path = _write_pyproject(tmp_path, block)
    assert mod.main(["--pyproject", str(path)]) == 1


# ---------------------------------------------------------------------------
# ALLOW: occ-only override (the current legitimate state)
# ---------------------------------------------------------------------------


def test_allow_occ_only_override(mod, tmp_path: Path) -> None:
    """onex-change-control git pin is intentional (immutable-main model)."""
    block = (
        "[tool.uv.sources]\n"
        'onex-change-control = { git = "https://github.com/OmniNode-ai/onex_change_control.git", '
        'rev = "2dd26ade7caaa7131e532473ec9d8a207d0e77ab" }\n'
    )
    path = _write_pyproject(tmp_path, block)

    assert mod.find_violations(path.read_text()) == []
    assert mod.main(["--pyproject", str(path)]) == 0


def test_allow_no_uv_sources_block(mod, tmp_path: Path) -> None:
    """A pyproject with no [tool.uv.sources] block cannot override anything."""
    content = (
        "[project]\n"
        'name = "x"\n'
        'version = "0.0.0"\n'
        'dependencies = ["omnibase-core>=0.46.1,<0.47.0"]\n'
        "[tool.ruff]\n"
        'target-version = "py312"\n'
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(content)
    assert mod.main(["--pyproject", str(path)]) == 0


def test_allow_pypi_only(mod, tmp_path: Path) -> None:
    """All first-party deps resolved from PyPI (no uv.sources entries) → clean."""
    block = "[tool.uv.sources]\n"
    path = _write_pyproject(tmp_path, block)
    assert mod.main(["--pyproject", str(path)]) == 0


# ---------------------------------------------------------------------------
# ESCAPE: `# raw-override-ok: <ticket>` exempts a line only with a non-empty token
# ---------------------------------------------------------------------------


def test_escape_valid_token(mod, tmp_path: Path) -> None:
    block = (
        "[tool.uv.sources]\n"
        'omnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", '
        'rev = "deadbeef" }  # raw-override-ok: OMN-12549\n'
    )
    path = _write_pyproject(tmp_path, block)

    assert mod.find_violations(path.read_text()) == []
    assert mod.main(["--pyproject", str(path)]) == 0


def test_escape_empty_token_still_fails(mod, tmp_path: Path) -> None:
    """`# raw-override-ok:` with no token does NOT exempt — gate still fails."""
    block = (
        "[tool.uv.sources]\n"
        'omnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", '
        'rev = "deadbeef" }  # raw-override-ok:\n'
    )
    path = _write_pyproject(tmp_path, block)

    assert len(mod.find_violations(path.read_text())) == 1
    assert mod.main(["--pyproject", str(path)]) == 1


def test_escape_is_per_line(mod, tmp_path: Path) -> None:
    """An escape on one line does not exempt an un-annotated override on another."""
    block = (
        "[tool.uv.sources]\n"
        'omnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", '
        'rev = "aaa" }  # raw-override-ok: OMN-12549\n'
        'omnibase-spi = { git = "https://github.com/OmniNode-ai/omnibase_spi.git", '
        'rev = "bbb" }\n'
    )
    path = _write_pyproject(tmp_path, block)

    violations = mod.find_violations(path.read_text())
    assert len(violations) == 1
    assert "omnibase-spi" in violations[0]
    assert mod.main(["--pyproject", str(path)]) == 1


# ---------------------------------------------------------------------------
# Missing file → hard error (exit 1, fail-closed)
# ---------------------------------------------------------------------------


def test_missing_pyproject_fails_closed(mod, tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "pyproject.toml"
    assert mod.main(["--pyproject", str(missing)]) == 1


DEV_TAG = "dev-20261019090050-c330f5a7d597"
SIBLING_PACKAGES = {
    "omnibase-core": "omnibase_core",
    "omnibase-spi": "omnibase_spi",
    "omnibase-compat": "omnibase_compat",
    "omnibase-infra": "omnibase_infra",
    "omninode-memory": "omnimemory",
    "omninode-intelligence": "omniintelligence",
    "omninode-claude": "omniclaude",
}


@pytest.mark.parametrize(("package", "repo"), SIBLING_PACKAGES.items())
@pytest.mark.parametrize("suffix", ["", ".git"])
def test_allow_sibling_dev_merge_tag_without_suppression(
    mod, tmp_path: Path, package: str, repo: str, suffix: str
) -> None:
    path = _write_pyproject(
        tmp_path,
        "[tool.uv.sources]\n"
        f'{package} = {{ git = "https://github.com/OmniNode-ai/{repo}{suffix}", '
        f'tag = "{DEV_TAG}" }}\n',
    )
    assert mod.find_violations(path.read_text()) == []
    assert mod.main(["--pyproject", str(path)]) == 0


@pytest.mark.parametrize(("package", "repo"), SIBLING_PACKAGES.items())
@pytest.mark.parametrize(
    "selector",
    [
        'branch = "dev"',
        'rev = "c330f5a7d597' + "a" * 28 + '"',
        f'rev = "{DEV_TAG}"',
        'tag = "v0.47.49"',
        'tag = "dev-latest"',
        'tag = "dev-20261019090050-c330f5a7d59"',
        'tag = "dev-20261019090050-C330F5A7D597"',
        f'tag = "{DEV_TAG}", branch = "dev"',
        f'tag = "{DEV_TAG}", rev = "c330f5a7d597"',
    ],
)
def test_dev_merge_tag_exception_refuses_other_selectors(
    mod, tmp_path: Path, package: str, repo: str, selector: str
) -> None:
    path = _write_pyproject(
        tmp_path,
        "[tool.uv.sources]\n"
        f'{package} = {{ git = "https://github.com/OmniNode-ai/{repo}.git", '
        f"{selector} }}\n",
    )
    assert mod.main(["--pyproject", str(path)]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/omnibase_core.git",
        "https://github.com/another-owner/omnibase_core.git",
        "https://github.com/OmniNode-ai/omnibase_spi.git",
        "https://github.com/OmniNode-ai/not-a-sibling.git",
        "https://github.com/OmniNode-ai/omnibase_core.git/extra",
        "https://github.com/OmniNode-ai/omnibase_core.git?branch=dev",
    ],
)
def test_dev_merge_tag_does_not_exempt_a_foreign_repository(
    mod, tmp_path: Path, url: str
) -> None:
    path = _write_pyproject(
        tmp_path,
        f'[tool.uv.sources]\nomnibase-core = {{ git = "{url}", tag = "{DEV_TAG}" }}\n',
    )
    assert mod.main(["--pyproject", str(path)]) == 1


def test_dev_merge_tag_does_not_exempt_a_non_sibling_package(mod) -> None:
    text = (
        "[tool.uv.sources]\n"
        'requests = { git = "https://github.com/psf/requests.git", '
        f'tag = "{DEV_TAG}" }}\n'
    )
    assert len(mod.find_violations(text)) == 1


@pytest.mark.parametrize(
    "source",
    [
        f"{{ git = 'https://github.com/OmniNode-ai/omnibase_core.git', tag = '{DEV_TAG}' }}",
        f'[{{ git = "https://github.com/OmniNode-ai/omnibase_core.git", tag = "{DEV_TAG}", marker = "sys_platform == \'linux\'" }}]',
    ],
)
def test_dev_merge_tag_uses_toml_semantics(mod, source: str) -> None:
    assert mod.find_violations(f"[tool.uv.sources]\nomnibase_core = {source}\n") == []


def test_dev_merge_tag_in_a_dotted_table(mod) -> None:
    text = (
        "[tool.uv.sources.omnibase_core]\n"
        'git = "https://github.com/OmniNode-ai/omnibase_core.git"\n'
        f'tag = "{DEV_TAG}"\n'
    )
    assert mod.find_violations(text) == []


def test_branch_in_a_dotted_table_is_refused(mod) -> None:
    text = (
        "[tool.uv.sources.omnibase_core]\n"
        'git = "https://github.com/OmniNode-ai/omnibase_core.git"\n'
        'branch = "dev"\n'
    )
    assert len(mod.find_violations(text)) == 1


def test_each_marker_source_must_qualify_independently(mod) -> None:
    text = (
        "[tool.uv.sources]\nomnibase-core = [\n"
        '{ git = "https://github.com/OmniNode-ai/omnibase_core.git", '
        f'tag = "{DEV_TAG}", marker = "sys_platform == \'linux\'" }},\n'
        '{ git = "https://github.com/OmniNode-ai/omnibase_core.git", '
        'branch = "dev", marker = "sys_platform == \'darwin\'" },\n]\n'
    )
    violations = mod.find_violations(text)
    assert len(violations) == 1
    assert "branch='dev'" in violations[0]


def test_invalid_toml_fails_closed(mod) -> None:
    assert mod.find_violations('[tool.uv.sources]\nomnibase-core = { git = "')
