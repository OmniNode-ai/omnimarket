# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""T2 AC9: a supported Linux client proceeds; unknown hosts fail closed.

Failure controls catch the former Darwin-only gate, accepting ID_LIKE clones,
ignoring an unreadable distribution identity, and misleading Windows advice.
Only external platform observations are supplied; collection and verdicts are real.
"""

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers import (
    handler_setup_preflight as preflight,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "kernel", ["6.8.0-generic", "5.15.153.1-microsoft-standard-WSL2"]
)
def test_preflight_linux_and_wsl_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel: str
) -> None:
    """The old macOS-only gate must fail both supported Ubuntu observations."""
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.platform, "release", lambda: kernel)
    monkeypatch.setattr(preflight.platform, "mac_ver", lambda: ("", ("", "", ""), ""))
    monkeypatch.setattr(
        preflight.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"}
    )
    result = preflight.HandlerSetupPreflight().handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "success", result
    assert result.evidence["linux_distribution"] == "ubuntu"
    assert result.changed is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "release",
    [
        {"ID": "debian"},
        {"ID": "linuxmint", "ID_LIKE": "ubuntu"},
        {},
        {"ID_LIKE": "ubuntu"},
    ],
)
def test_unlisted_linux_distribution_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: dict[str, str]
) -> None:
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.platform, "freedesktop_os_release", lambda: release)
    result = preflight.HandlerSetupPreflight().handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "unavailable"
    assert "Ubuntu" in (result.fix or "")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "error",
    [OSError("private-release-sentinel"), UnicodeError("private-release-sentinel")],
)
def test_unreadable_linux_identity_is_a_secret_free_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")

    def unreadable() -> dict[str, str]:
        raise error

    monkeypatch.setattr(preflight.platform, "freedesktop_os_release", unreadable)
    result = preflight.HandlerSetupPreflight().handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "failure"
    assert "private-release-sentinel" not in result.model_dump_json()
    assert "preflight" in (result.fix or "")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("system", ["Windows", "FreeBSD"])
def test_unlisted_operating_system_remains_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str
) -> None:
    monkeypatch.setattr(preflight.platform, "system", lambda: system)
    result = preflight.HandlerSetupPreflight().handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "unavailable"
    if system == "Windows":
        assert "WSL 2" in (result.fix or "")
        assert "Ubuntu" in (result.fix or "")
    assert list(tmp_path.iterdir()) == []
