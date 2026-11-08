"""The setup verb's secret store target and identity resolution (OMN-22870)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_adapters import (
    InfisicalSetupSecrets,
    SetupStoreTarget,
    load_store_target,
    read_identity,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_ports import (
    SetupStoreError,
)


@pytest.mark.unit
def test_target_is_the_contract_declared_one() -> None:
    target = load_store_target()
    assert target.project_id == "872e00f4-1279-49a4-8e16-a2bd961f4996"
    assert target.environment == "dev"
    assert target.folder == "/shared/env"


@pytest.mark.unit
def test_env_file_identity_wins_over_a_stale_shell(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text(
        'INFISICAL_ADDR="http://file:8880"\nexport INFISICAL_CLIENT_ID=fileid\n'
        "INFISICAL_CLIENT_SECRET='filesecret'\nINFISICAL_PROJECT_ID=other\nUNRELATED=1\n"
    )
    got = read_identity(
        str(f), {"INFISICAL_CLIENT_ID": "shellid", "INFISICAL_ADDR": "http://shell"}
    )
    assert got == {
        "INFISICAL_ADDR": "http://file:8880",
        "INFISICAL_CLIENT_ID": "fileid",
        "INFISICAL_CLIENT_SECRET": "filesecret",
    }


@pytest.mark.unit
def test_shell_fills_what_a_missing_file_lacks(tmp_path: Path) -> None:
    ambient = {"INFISICAL_ADDR": "a", "INFISICAL_CLIENT_ID": "b"}
    assert read_identity(str(tmp_path / "none"), ambient) == ambient


@pytest.mark.unit
def test_missing_identity_names_only_the_variables(tmp_path: Path) -> None:
    target = SetupStoreTarget(
        "/shared/env",
        "872e00f4-1279-49a4-8e16-a2bd961f4996",
        "dev",
        str(tmp_path / "none"),
    )
    with pytest.raises(SetupStoreError, match="INFISICAL_CLIENT_SECRET"):
        InfisicalSetupSecrets(
            target, {"INFISICAL_ADDR": "x", "INFISICAL_CLIENT_ID": "y"}
        )
