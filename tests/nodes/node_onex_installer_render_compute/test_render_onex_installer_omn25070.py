# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_onex_installer_render_compute (OMN-25070).

Failure modes each test catches: a placeholder left in the released script or a
pinned value not substituted; a checksum table missing a platform or carrying a
malformed digest; a version, Python or launcher value that would produce a
broken install line; an unknown field slipping through; the published sha256
not matching the script; and the runtime command writing a script for an
invalid request.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_onex_installer_render_compute.handlers.handler_onex_installer_render import (
    HandlerOnexInstallerRender,
)
from omnimarket.nodes.node_onex_installer_render_compute.models.model_onex_installer import (
    UV_TARGETS,
    ModelOnexInstallerRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUEST_FILE = REPO_ROOT / "packages" / "omninode-onex" / "installer.json"
RUNTIME = (
    "omnimarket.nodes.node_onex_installer_render_compute.runtime_onex_installer_render"
)


def _request_data() -> dict[str, Any]:
    data = json.loads(REQUEST_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.mark.unit
def test_render_substitutes_every_pinned_value() -> None:
    data = _request_data()
    result = HandlerOnexInstallerRender().handle(
        ModelOnexInstallerRequest.model_validate(data)
    )
    script = result.script
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "@" + "VERSION@" not in script
    assert all(f"@{name}@" not in script for name in ("PACKAGE", "PYTHON", "LAUNCHERS"))
    assert f'ONEX_VERSION="{data["version"]}"' in script
    assert f'ONEX_PACKAGE="{data["package"]}"' in script
    assert f'ONEX_LAUNCHERS="{" ".join(data["launchers"])}"' in script
    assert f'UV_VERSION="{data["uv_version"]}"' in script
    for target in UV_TARGETS:
        assert f'{target}) echo "{data["uv_sha256"][target]}" ;;' in script
    assert result.sha256 == hashlib.sha256(script.encode("utf-8")).hexdigest()
    assert 'main "$@"' in script.rstrip().splitlines()[-1]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "latest"),
        ("version", "0.0.1; rm -rf ~"),
        ("python", "python3"),
        ("package", "Bad Name"),
        ("launchers", []),
        ("launchers", ["onex", "onex"]),
        ("launchers", ["onex", "$(id)"]),
        ("uv_version", "latest"),
    ],
)
def test_a_malformed_value_is_refused(field: str, value: object) -> None:
    data = _request_data()
    data[field] = value
    with pytest.raises(ValidationError):
        ModelOnexInstallerRequest.model_validate(data)


@pytest.mark.unit
def test_a_missing_platform_checksum_is_refused() -> None:
    data = _request_data()
    del data["uv_sha256"]["x86_64-apple-darwin"]
    with pytest.raises(ValidationError, match="must name exactly"):
        ModelOnexInstallerRequest.model_validate(data)


@pytest.mark.unit
def test_a_malformed_checksum_is_refused() -> None:
    data = _request_data()
    data["uv_sha256"]["aarch64-unknown-linux-gnu"] = "ABC"
    with pytest.raises(ValidationError, match="64 lowercase hex"):
        ModelOnexInstallerRequest.model_validate(data)


@pytest.mark.unit
def test_an_unknown_field_is_refused() -> None:
    data = _request_data()
    data["index_url"] = "https://example.invalid/simple"
    with pytest.raises(ValidationError):
        ModelOnexInstallerRequest.model_validate(data)


@pytest.mark.unit
def test_runtime_writes_the_script_and_refuses_a_bad_request() -> None:
    good = subprocess.run(
        [sys.executable, "-m", RUNTIME],
        input=REQUEST_FILE.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert good.returncode == 0, good.stderr
    expected = HandlerOnexInstallerRender().handle(
        ModelOnexInstallerRequest.model_validate(_request_data())
    )
    assert good.stdout == expected.script

    bad = _request_data()
    bad["version"] = "latest"
    refused = subprocess.run(
        [sys.executable, "-m", RUNTIME],
        input=json.dumps(bad),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert refused.returncode == 2
    assert refused.stdout == ""
    assert "invalid onex installer request" in refused.stderr
