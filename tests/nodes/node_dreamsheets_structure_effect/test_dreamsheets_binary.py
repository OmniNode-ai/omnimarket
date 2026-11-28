# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node against the REAL DreamSheets binary, plus the no-leak test.

Opt-in, because the binary is not in this repo:

``ONEX_DREAMSHEETS_TEST_BINARY``
    a ``dreamsheets-desktop`` executable (any OS). Runs it directly (no
    sandbox) to check the protocol, the records and the no-leak rule.
``ONEX_DREAMSHEETS_TEST_FIXTURES``
    the ``fixtures/`` dir of the DreamSheets OmniNode kit.
``ONEX_DREAMSHEETS_APPIMAGE_ROOT`` (Linux with bubblewrap)
    an extracted AppImage: the same checks then also run SANDBOXED, through
    the node's own default launcher.

NO-LEAK. A document is compiled with a unique marker in every place a name or
text can live; the binary's own ``export`` proves the markers are in the file
(the positive control); then no marker may appear anywhere in the node's
result, in either privacy mode, nor when the path itself carries the marker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnimarket.nodes.node_dreamsheets_structure_effect import (
    BwrapDreamsheetsLauncher,
    EnumDreamsheetsStructureStatus,
    HandlerDreamsheetsStructureEffect,
    ModelDreamsheetsStructureRequest,
    ModelDreamsheetsStructureResult,
)
from omnimarket.nodes.node_dreamsheets_structure_effect.protocols.sandbox_launcher import (
    DREAMSHEETS_ARGS,
    ProtocolSandboxLauncher,
    RunLayout,
)

BINARY = os.environ.get("ONEX_DREAMSHEETS_TEST_BINARY")
FIXTURES = os.environ.get("ONEX_DREAMSHEETS_TEST_FIXTURES")
APPIMAGE = os.environ.get("ONEX_DREAMSHEETS_APPIMAGE_ROOT")
SANDBOX = (
    sys.platform == "linux" and shutil.which("bwrap") is not None and bool(APPIMAGE)
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not BINARY, reason="set ONEX_DREAMSHEETS_TEST_BINARY"),
]

MARK = "ZQX{}7F3A"
LOCATIONS = [
    "title",
    "tab",
    "table",
    "column",
    "tabledesc",
    "columndesc",
    "calcformula",
    "sqltext",
    "sqltable",
    "tile",
    "tileformula",
    "chart",
    "customfn",
    "customfnbody",
    "ainotes",
    "cell",
]


@dataclass(frozen=True)
class DirectLauncher:
    """The real binary, unsandboxed, with the node's own flags."""

    exe: Path

    @property
    def binary(self) -> Path:
        return self.exe

    def document_path(self, layout: RunLayout) -> str:
        return str(layout.input_file)

    def command(self, layout: RunLayout) -> list[str]:
        args = [
            str(layout.config_dir) if a == "/config" else a for a in DREAMSHEETS_ARGS
        ]
        return [str(self.exe), *args]

    def environment(self, layout: RunLayout) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "TMPDIR": str(layout.tmp_dir),
            "HOME": str(layout.home_dir),
        }
        if sys.platform == "win32":
            env |= {
                "SYSTEMROOT": os.environ["SYSTEMROOT"],
                "TEMP": str(layout.tmp_dir),
                "TMP": str(layout.tmp_dir),
            }
        return env


def launchers() -> list[ProtocolSandboxLauncher]:
    found: list[ProtocolSandboxLauncher] = [DirectLauncher(Path(BINARY or "."))]
    if SANDBOX:
        found.append(BwrapDreamsheetsLauncher(Path(APPIMAGE or ".")))
    return found


def summarize(
    launcher: ProtocolSandboxLauncher, path: Path, tmp: Path, values: bool = False
) -> ModelDreamsheetsStructureResult:
    handler = HandlerDreamsheetsStructureEffect(
        launcher, values_withheld=not values, run_base_dir=tmp / "runs"
    )
    return handler.run_sync(
        ModelDreamsheetsStructureRequest(document_path=str(path.resolve()))
    )


def marker_document(tmp: Path) -> Path:
    m = {loc: MARK.format(loc.upper()) for loc in LOCATIONS}
    dsdoc = {
        "title": m["title"],
        "aiNotes": f"notes {m['ainotes']}",
        "customFunctions": [
            {
                "name": m["customfn"],
                "params": ["x"],
                "body": f'IF(x = "{m["customfnbody"]}", 1, 2)',
            }
        ],
        "tabs": [
            {
                "name": m["tab"],
                "tables": [
                    {
                        "name": m["table"],
                        "description": m["tabledesc"],
                        "columns": [
                            {
                                "name": m["column"],
                                "type": "text",
                                "description": m["columndesc"],
                            },
                            {
                                "name": "Flag",
                                "formula": f'IF(@{m["column"]} = "{m["calcformula"]}", 1, 0)',
                            },
                        ],
                        "rows": [[m["cell"]], ["plain"]],
                    },
                    {
                        "name": m["sqltable"],
                        "sql": f"SELECT * FROM {m['table']} WHERE {m['column']} <> '{m['sqltext']}'",
                    },
                ],
                "tiles": [
                    {
                        "name": m["tile"],
                        "formula": f'COUNTIF({m["table"]}.{m["column"]}, "{m["tileformula"]}")',
                    }
                ],
                "charts": [
                    {
                        "name": m["chart"],
                        "type": "bar",
                        "table": m["table"],
                        "x": m["column"],
                        "y": ["Flag"],
                    }
                ],
            }
        ],
    }
    src = tmp / "markers.json"
    src.write_text(json.dumps(dsdoc))
    out = tmp / MARK.format("DIR") / f"{MARK.format('FILE')}.dsheet"
    out.parent.mkdir()
    subprocess.run(
        [BINARY or "", "compile", str(src), "-o", str(out)],
        check=True,
        capture_output=True,
    )
    return out


def test_no_marker_leaves_in_any_mode(tmp_path: Path) -> None:
    doc = marker_document(tmp_path)
    exported = subprocess.run(
        [BINARY or "", "export", str(doc)], check=True, capture_output=True, text=True
    ).stdout
    in_file = [loc for loc in LOCATIONS if MARK.format(loc.upper()) in exported]
    assert in_file == LOCATIONS, (
        f"positive control: markers missing from the file: {set(LOCATIONS) - set(in_file)}"
    )
    for launcher in launchers():
        for values in (False, True):
            result = summarize(launcher, doc, tmp_path, values)
            assert result.status is EnumDreamsheetsStructureStatus.COMPLETED, result
            text = result.model_dump_json()
            assert "ZQX" not in text, (type(launcher).__name__, values, text)
            assert result.summary is not None
            assert result.summary.tables == 2


def test_open_errors_are_bare_codes(tmp_path: Path) -> None:
    junk = tmp_path / MARK.format("JUNKDIR") / "junk.dsheet"
    junk.parent.mkdir()
    junk.write_text(MARK.format("JUNKBODY") + " not a zip")
    for launcher in launchers():
        result = summarize(launcher, junk, tmp_path)
        assert result.status is EnumDreamsheetsStructureStatus.FAILED
        assert (result.error_code or "").startswith("E_OPEN_"), result
        assert "ZQX" not in result.model_dump_json()


@pytest.mark.skipif(not FIXTURES, reason="set ONEX_DREAMSHEETS_TEST_FIXTURES")
@pytest.mark.parametrize(
    ("name", "bucket"),
    [
        ("empty", "empty"),
        ("cells-999", "xs"),
        ("cells-1000", "s"),
        ("cells-99999", "s"),
        ("cells-100000", "m"),
        ("cells-999999", "m"),
        ("cells-1000000", "l"),
    ],
)
def test_size_buckets(tmp_path: Path, name: str, bucket: str) -> None:
    path = Path(FIXTURES or ".") / f"{name}.dsheet"
    for launcher in launchers():
        values = summarize(launcher, path, tmp_path, values=True)
        assert values.summary is not None
        assert values.summary.size_bucket == bucket, values
        strict = summarize(launcher, path, tmp_path)
        assert strict.summary is not None
        assert (
            strict.summary.rows,
            strict.summary.cells,
            strict.summary.size_bucket,
            strict.summary.largest_table,
        ) == (None, None, None, None)


@pytest.mark.skipif(not FIXTURES, reason="set ONEX_DREAMSHEETS_TEST_FIXTURES")
@pytest.mark.parametrize(
    ("name", "field", "value"),
    [
        ("cap-sql-table", "sql_tables", 1),
        ("cap-calc-column", "calc_columns", 1),
        ("cap-tile", "tiles", 1),
        ("cap-chart", "charts", 1),
        ("cap-custom-function", "custom_functions", 1),
        ("cap-script", "scripts", 1),
        ("cap-ai-notes", "ai_notes", True),
    ],
)
def test_capabilities_in_isolation(
    tmp_path: Path, name: str, field: str, value: object
) -> None:
    base = summarize(
        DirectLauncher(Path(BINARY or ".")),
        Path(FIXTURES or ".") / "cap-none.dsheet",
        tmp_path,
    )
    assert base.summary is not None
    for launcher in launchers():
        result = summarize(launcher, Path(FIXTURES or ".") / f"{name}.dsheet", tmp_path)
        assert result.summary is not None, result
        caps = result.summary.capabilities.model_dump()
        assert caps[field] == value
        others = {k: v for k, v in caps.items() if k != field}
        assert others == {
            k: v
            for k, v in base.summary.capabilities.model_dump().items()
            if k != field
        }
