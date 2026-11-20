# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin the observable output of the ``onex-capability-catalog`` console entry point.

The tests reach the code only through the console entry point name in ``pyproject.toml``
(resolved through package metadata, never through a module path), with stdout, stderr and the
exit code compared byte for byte against recorded goldens.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).parent / "golden"
CATALOG_FIXTURE = Path(__file__).parent / "fixtures" / "catalog_2026-09-27.json"

CATALOG_ENTRY = "onex-capability-catalog"
PUBLISH_ENTRY = "onex-capability-catalog-publish"
RENDER_FILES = (
    "capability-catalog.md",
    "digest.txt",
    "brief-part.md",
    "claude-md-block.md",
)


def _entry(name: str) -> Callable[..., int]:
    (ep,) = tuple(entry_points(group="console_scripts", name=name))
    main = ep.load()
    assert callable(main)
    return cast(Callable[..., int], main)


def _run(
    capsys: pytest.CaptureFixture[str], name: str, argv: list[str]
) -> tuple[int | str, str, str]:
    main = _entry(name)
    try:
        code: int | str = main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, (int, str)) else 0
    out, err = capsys.readouterr()
    return code, out, err


def _golden(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def _scrub(text: str, tmp_path: Path) -> str:
    return text.replace(str(tmp_path), "<tmp>")


def test_catalog_render_writes_the_recorded_views(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out_dir = tmp_path / "out"
    code, out, err = _run(
        capsys,
        CATALOG_ENTRY,
        [
            "render",
            "--in",
            str(CATALOG_FIXTURE),
            "--out-dir",
            str(out_dir),
            "--target",
            "claude-md",
        ],
    )
    assert (code, _scrub(out, tmp_path), err) == (0, "rendered to <tmp>/out\n", "")
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(RENDER_FILES)
    for name in RENDER_FILES:
        assert (out_dir / name).read_text(encoding="utf-8") == _golden(
            f"catalog_render/{name}"
        )


def test_catalog_render_without_target_writes_three_views(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, _out, _err = _run(
        capsys,
        CATALOG_ENTRY,
        ["render", "--in", str(CATALOG_FIXTURE), "--out-dir", str(tmp_path)],
    )
    assert code == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(RENDER_FILES[:3])


@pytest.mark.parametrize(
    ("golden", "extra"),
    [
        ("digest_all.txt", []),
        ("digest_pool_engines.txt", ["--section", "pool,engines"]),
        ("digest_for_brief.txt", ["--for-brief"]),
    ],
)
def test_catalog_digest_prints_the_recorded_text(
    capsys: pytest.CaptureFixture[str], golden: str, extra: list[str]
) -> None:
    code, out, err = _run(
        capsys, CATALOG_ENTRY, ["digest", "--in", str(CATALOG_FIXTURE), *extra]
    )
    assert (code, err) == (0, "")
    assert out == _golden(f"catalog_cli/{golden}")


SHOW_SECTIONS = (
    "clis",
    "credentials",
    "docs",
    "engines",
    "hosts",
    "jobs",
    "lanes",
    "mcp_servers",
    "model_servers",
    "plugins",
    "skills",
)


@pytest.mark.parametrize("section", SHOW_SECTIONS)
def test_catalog_show_prints_the_recorded_section(
    capsys: pytest.CaptureFixture[str], section: str
) -> None:
    code, out, err = _run(
        capsys, CATALOG_ENTRY, ["show", section, "--in", str(CATALOG_FIXTURE)]
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == json.loads(_golden(f"catalog_cli/show_{section}.json"))
    assert out == _golden(f"catalog_cli/show_{section}.json")


def test_catalog_show_jobs_filters_by_state(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = _run(
        capsys,
        CATALOG_ENTRY,
        ["show", "jobs", "--state", "dead", "--in", str(CATALOG_FIXTURE)],
    )
    assert code == 0
    assert out == _golden("catalog_cli/show_jobs_dead.json")


def test_catalog_show_reads_the_env_catalog_path(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_CAPABILITY_CATALOG_JSON", str(CATALOG_FIXTURE))
    code, out, _err = _run(capsys, CATALOG_ENTRY, ["show", "hosts"])
    assert code == 0
    assert out == _golden("catalog_cli/show_hosts.json")


def test_catalog_without_a_catalog_path_fails_fast(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_CAPABILITY_CATALOG_JSON", raising=False)
    code, out, err = _run(capsys, CATALOG_ENTRY, ["digest"])
    assert code == (
        "no catalog.json given: pass --in <path> or set ONEX_CAPABILITY_CATALOG_JSON "
        "(fail-fast, no silent default path)"
    )
    assert (out, err) == ("", "")


@pytest.mark.parametrize("term", ["codex", "h908", "glm"])
def test_catalog_find_prints_the_recorded_hits(
    capsys: pytest.CaptureFixture[str], term: str
) -> None:
    code, out, err = _run(
        capsys, CATALOG_ENTRY, ["find", term, "--in", str(CATALOG_FIXTURE)]
    )
    assert (code, out, err) == (*json.loads(_golden(f"catalog_cli/find_{term}.json")),)


def test_catalog_find_miss_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(
        capsys, CATALOG_ENTRY, ["find", "zz-no-such-term", "--in", str(CATALOG_FIXTURE)]
    )
    assert (code, out, err) == (
        1,
        "",
        "no match for 'zz-no-such-term' in the capability catalog\n",
    )


def test_catalog_collect_refuses_a_missing_mode(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # CA3 now supplies --tools; its bus adapter is exercised by test_collect_tools.py.
    assert _run(capsys, CATALOG_ENTRY, ["collect", "--out-dir", str(tmp_path)]) == (
        2,
        "",
        "collect: pass --lab and/or --tools\n",
    )


def test_catalog_unknown_verb_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run(capsys, CATALOG_ENTRY, ["frobnicate"])
    assert code == 2
    assert out == ""
    assert "invalid choice: 'frobnicate'" in err


def test_catalog_help_lists_the_recorded_verbs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _err = _run(capsys, CATALOG_ENTRY, ["--help"])
    assert code == 0
    assert out == _golden("catalog_cli/help.txt")


def test_publish_help_lists_the_recorded_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _err = _run(capsys, PUBLISH_ENTRY, ["--help"])
    assert code == 0
    # argparse names the program after sys.argv[0]; everything after the usage line is pinned.
    body = out.split("\n", 1)[1]
    options = re.sub(r"\s+", " ", body[body.index("--omni-home") :]).strip()
    assert options == _golden("catalog_cli/publish_options.txt").strip()
