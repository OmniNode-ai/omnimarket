# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Configuration portability must not copy credentials or mutate live setup.

Failures: unknown plugin, invalid schema, secret-bearing input, profile traversal,
symlink replacement, concurrent setup, and silent overwrite of another profile.
"""

import fcntl
import importlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _run(home: Path, operation: str, **values: object) -> object:
    model = importlib.import_module(
        "omnimarket.nodes.node_onex_setup_effect.models.model_setup_configuration"
    )
    owner = importlib.import_module(
        "omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_configuration"
    )
    return owner.HandlerSetupConfiguration(onex_home=home).handle(
        model.ModelSetupConfigurationRequest(operation=operation, **values)
    )


def _source(tmp_path: Path, **values: object) -> Path:
    source = tmp_path / "profile.json"
    source.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path / "workspace"),
                "components": ["delegation"],
                **values,
            }
        )
    )
    return source


def test_import_is_validated_nonsecret_noop_and_not_live_setup(tmp_path: Path) -> None:
    home = tmp_path / "onex"
    source = _source(tmp_path)
    result = _run(home, "import", source=source, name="work")
    assert result.status == "ready"
    assert result.changed
    saved = home / "profiles" / "work.json"
    before = saved.read_bytes(), saved.stat().st_mtime_ns
    again = _run(home, "import", source=source, name="work")
    assert not again.changed
    assert (saved.read_bytes(), saved.stat().st_mtime_ns) == before
    assert not (home / "setup.json").exists()
    assert _run(home, "list").profiles == ("work",)
    exported = _run(home, "export", name="work")
    assert exported.profile.plan.workspace == tmp_path / "workspace"
    assert exported.source == saved


@pytest.mark.parametrize(
    "bad",
    [
        {"api_key": "sentinel-never-export"},
        {"components": ["not-installed"]},
        {"schema_version": 999},
    ],
)
def test_invalid_profile_refused_without_state_or_secret(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    home = tmp_path / "onex"
    source = _source(tmp_path, **bad)
    result = _run(home, "import", source=source, name="work")
    assert result.status == "refused"
    assert "sentinel-never-export" not in result.model_dump_json()
    assert not home.exists()
    # Positive control: the input scanner can see the planted secret.
    if "api_key" in bad:
        assert "sentinel-never-export" in source.read_text()


def test_import_preserves_existing_profile_without_explicit_replace(
    tmp_path: Path,
) -> None:
    home = tmp_path / "onex"
    source = _source(tmp_path)
    _run(home, "import", source=source, name="work")
    saved = home / "profiles" / "work.json"
    before = saved.read_bytes()
    _source(tmp_path, developer=True)
    assert _run(home, "import", source=source, name="work").status == "refused"
    assert saved.read_bytes() == before
    assert _run(home, "import", source=source, name="work", replace=True).changed


def test_import_refuses_symlink_and_concurrent_setup(tmp_path: Path) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    source = _source(tmp_path)
    with (home / "setup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _run(home, "import", source=source, name="work").status == "refused"
        assert not (home / "profiles").exists()
    profiles = home / "profiles"
    profiles.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("keep")
    (profiles / "work.json").symlink_to(unrelated)
    assert (
        _run(home, "import", source=source, name="work", replace=True).status
        == "refused"
    )
    assert unrelated.read_text() == "keep"


def test_profile_name_cannot_escape_home(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="name"):
        _run(tmp_path / "onex", "import", source=_source(tmp_path), name="../outside")
    assert not (tmp_path / "outside.json").exists()
