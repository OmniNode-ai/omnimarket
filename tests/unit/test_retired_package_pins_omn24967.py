"""The lockfile and pyproject do not name the retired package; the pin comments say it is retired."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_RETIRED = re.compile("omnibase" + "[_-]" + "comp" + "at", re.IGNORECASE)


def test_lockfile_and_pyproject_do_not_name_the_retired_package() -> None:
    for name in ("uv.lock", "pyproject.toml"):
        hits = [
            line
            for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
            if _RETIRED.search(line)
        ]
        assert not hits, f"{name}: {hits}"


def test_both_pin_comment_blocks_say_since_retired_package() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert text.count("since-retired package") >= 2
