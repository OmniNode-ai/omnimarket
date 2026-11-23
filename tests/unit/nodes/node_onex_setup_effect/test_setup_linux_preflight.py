# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""T2 AC9: a supported Linux client proceeds; unknown hosts fail closed.

Failure controls catch the former Darwin-only gate, accepting ID_LIKE clones,
ignoring an unreadable distribution identity, and misleading Windows advice.
Only external platform observations are supplied; collection and verdicts are real.
"""

import builtins
import io
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers import (
    handler_setup_preflight as preflight,
)
from omnimarket.nodes.node_onex_setup_effect.handlers import (
    setup_contract_values as contract_values,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "kernel",
    [
        "6.8.0-generic",
        "5.15.153.1-microsoft-standard-WSL2",
        "4.19.128-microsoft-standard",
        "5.15.0-Microsoft-standard-WSL2",
    ],
)
def test_preflight_linux_and_wsl_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel: str
) -> None:
    """The old macOS-only gate must fail native Ubuntu and Ubuntu under WSL 2.

    Microsoft's WSL 2 kernels carry the lowercase "-microsoft-standard" local
    version, without a WSL2 suffix on the 4.19 branch, so a WSL 1 check that
    ignores case or requires WSL2 must fail here. A release that names
    Microsoft with a capital M but also WSL2, such as a custom-built WSL 2
    kernel, must still be admitted, so a WSL 1 check that drops its "WSL2"
    exclusion fails here.
    """
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


@pytest.mark.parametrize("kernel", ["4.4.0-19041-Microsoft", "4.4.0-17763-Microsoft"])
def test_ubuntu_on_a_wsl1_kernel_is_refused_with_the_wsl2_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel: str
) -> None:
    """The delivery plan names Ubuntu under WSL 2 only; WSL 1 reports a
    capital-M Microsoft kernel release (microsoft/WSL#423)."""
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.platform, "release", lambda: kernel)
    monkeypatch.setattr(
        preflight.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"}
    )
    result = preflight.HandlerSetupPreflight().handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "unavailable", result
    assert "WSL 1" in (result.fix or "")
    assert "WSL 2" in (result.fix or "")
    assert result.evidence["linux_distribution"] == "ubuntu"
    assert list(tmp_path.iterdir()) == []


def test_contract_list_decides_which_distributions_are_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that ignores the contract and names Ubuntu itself must fail."""
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        preflight.platform, "freedesktop_os_release", lambda: {"ID": "fedora"}
    )
    plan = ModelSetupPlan(workspace=tmp_path / "workspace")
    shipped = preflight.HandlerSetupPreflight().handle(plan)
    assert shipped.status == "unavailable", shipped
    node = tmp_path / "node"
    node.mkdir()
    (node / "contract.yaml").write_text(
        "metadata:\n  native_linux_distributions: [ubuntu, fedora]\n"
    )
    contract_values._mapping.cache_clear()
    try:
        with monkeypatch.context() as patched:
            patched.setattr(contract_values, "_NODE", node)
            reviewed = preflight.HandlerSetupPreflight().handle(plan)
    finally:
        contract_values._mapping.cache_clear()
    assert reviewed.status == "success", reviewed
    assert reviewed.evidence["linux_distribution"] == "fedora"


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


@pytest.mark.parametrize("error", [PermissionError, IsADirectoryError])
def test_unreadable_etc_os_release_is_not_answered_by_the_vendor_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    """os-release(5) reads /usr/lib/os-release only when /etc/os-release is missing.

    The standard library falls back on any OSError, so an existing but unreadable
    host file would be answered by the vendor copy's identity.
    """
    real_open = builtins.open

    def host_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if file == "/etc/os-release":
            raise error(13, "unreadable", file)
        if file == "/usr/lib/os-release":
            return io.StringIO("ID=ubuntu\n")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(builtins, "open", host_open)
    result = preflight.HandlerSetupPreflight().handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "failure", result
    assert "linux_distribution" not in result.evidence
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fault", ["missing_drive", "unreadable_disk"])
def test_windows_remedy_needs_no_workspace_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """A Windows workspace on a missing mapped drive (Z:\\ is its own parent) or an
    unreadable disk must still give the WSL remedy, not a hang or a failure."""
    plan = ModelSetupPlan(workspace=tmp_path / "Code" / "omnibase")
    walked: list[Path] = []

    def missing(self: Path, **_: object) -> bool:
        walked.append(self)
        if len(walked) > 100:
            raise AssertionError("the workspace ancestor walk never stops")
        return False

    def unreadable(path: object) -> object:
        raise PermissionError(13, "Access is denied", str(path))

    # Path.exists is restored before pytest reports, which also reads paths.
    with monkeypatch.context() as patched:
        patched.setattr(preflight.platform, "system", lambda: "Windows")
        if fault == "missing_drive":
            patched.setattr(preflight.Path, "exists", missing)
        else:
            patched.setattr(preflight.shutil, "disk_usage", unreadable)
        result = preflight.HandlerSetupPreflight().handle(plan)
    assert result.status == "unavailable", result
    assert "WSL 2" in (result.fix or "")


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
