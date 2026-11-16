# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario settings are found under the names that already exist (OMN-23602).

Golden: the host file holds the existing names and each secret, template and literal setting fills, with no store asked.
Error chains: the environment's own value wins and is never looked up, the store is asked only for names the file lacks, a
store that raises leaves only that setting unresolved, a name found nowhere is reported by its variable (never skipped), and no
value reaches the unresolved result or the file read of other names. The failing control: an overlay naming a secret that
exists nowhere fills nothing, which is what every scenario did at base.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import (
    fill_declared_settings,
    read_host_env_names,
)
from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioSetting,
    ModelScenarioSettingsOverlay,
)

pytestmark = pytest.mark.unit

OVERLAY = ModelScenarioSettingsOverlay(
    folder="/shared/env",
    host_env_file=".omnibase/.env",
    settings=(
        ModelScenarioSetting(env="RUN_KEY", secret="EXISTING_KEY"),
        ModelScenarioSetting(
            env="RUN_PAIR", template="{EXISTING_URL}|{EXISTING_MODEL}"
        ),
        ModelScenarioSetting(env="RUN_LITERAL", value="http://127.0.0.1:9"),
    ),
)


class FakeStore:
    """The store seam: the values it holds, the names it is asked, the name that raises."""

    def __init__(self, values: dict[str, str], raise_for: str | None = None) -> None:
        self._values = values
        self._raise_for = raise_for
        self.asked: list[str] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        assert folder == "/shared/env"
        self.asked.append(name)
        if name == self._raise_for:
            raise RuntimeError("refused")
        value = self._values.get(name)
        return SecretStr(value) if value is not None else None


def _home(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".omnibase" / ".env"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return tmp_path


FILE = "EXISTING_KEY=fake-key-1\nEXISTING_URL=http://h:1/v1\nEXISTING_MODEL=m1\nOTHER=not-wanted\n"


def test_golden_every_setting_fills_from_the_host_file(tmp_path: Path) -> None:
    """The secret, the template and the literal all fill from the host file and the store is not asked."""
    store = FakeStore({})

    filled, unresolved = fill_declared_settings(
        {}, OVERLAY, home=_home(tmp_path, FILE), store=store
    )

    assert filled["RUN_KEY"] == "fake-key-1"
    assert filled["RUN_PAIR"] == "http://h:1/v1|m1"
    assert filled["RUN_LITERAL"] == "http://127.0.0.1:9"
    assert unresolved == ()
    assert store.asked == []


def test_the_environment_value_wins_and_its_name_is_not_looked_up(
    tmp_path: Path,
) -> None:
    """A variable the environment holds is kept and nothing is looked up for it."""
    store = FakeStore({})

    filled, _ = fill_declared_settings(
        {
            "RUN_KEY": "set-by-env",
            "EXISTING_URL": "http://e/v1",
            "EXISTING_MODEL": "me",
        },
        OVERLAY,
        home=_home(tmp_path, "EXISTING_KEY=from-file\n"),
        store=store,
    )

    assert filled["RUN_KEY"] == "set-by-env"
    assert filled["RUN_PAIR"] == "http://e/v1|me"


def test_the_store_is_asked_only_for_names_the_file_lacks(tmp_path: Path) -> None:
    """A name missing from the file is read from the store; a name the file holds is not asked."""
    store = FakeStore({"EXISTING_KEY": "from-store"})

    filled, unresolved = fill_declared_settings(
        {},
        OVERLAY,
        home=_home(tmp_path, "EXISTING_URL=http://h:1/v1\nEXISTING_MODEL=m1\n"),
        store=store,
    )

    assert filled["RUN_KEY"] == "from-store"
    assert unresolved == ()
    assert store.asked == ["EXISTING_KEY"]


def test_a_store_that_raises_leaves_only_that_setting_unresolved(
    tmp_path: Path,
) -> None:
    """The refused name leaves its setting unresolved and the other settings still fill."""
    store = FakeStore({}, raise_for="EXISTING_KEY")

    filled, unresolved = fill_declared_settings(
        {},
        OVERLAY,
        home=_home(tmp_path, "EXISTING_URL=http://h:1/v1\nEXISTING_MODEL=m1\n"),
        store=store,
    )

    assert unresolved == ("RUN_KEY",)
    assert "RUN_KEY" not in filled
    assert filled["RUN_PAIR"] == "http://h:1/v1|m1"


def test_a_blank_value_in_the_file_counts_as_absent(tmp_path: Path) -> None:
    """A name whose file value is blank is unresolved, never filled with an empty string."""
    filled, unresolved = fill_declared_settings(
        {}, OVERLAY, home=_home(tmp_path, FILE.replace("fake-key-1", ""))
    )

    assert unresolved == ("RUN_KEY",)
    assert "RUN_KEY" not in filled


def test_only_wanted_names_are_returned_and_quotes_and_export_are_handled(
    tmp_path: Path,
) -> None:
    """The file read returns the wanted names unquoted and no other name."""
    home = _home(tmp_path, "export EXISTING_KEY='fake-key-2'\nOTHER=\"not-wanted\"\n")

    found = read_host_env_names(home, ".omnibase/.env", frozenset({"EXISTING_KEY"}))

    assert found == {"EXISTING_KEY": "fake-key-2"}


def test_no_value_reaches_the_unresolved_result(tmp_path: Path) -> None:
    """The unresolved tuple holds variable names only."""
    _, unresolved = fill_declared_settings(
        {}, OVERLAY, home=_home(tmp_path, "EXISTING_KEY=fake-key-3\n")
    )

    assert unresolved == ("RUN_PAIR",)
    assert all("fake-key-3" not in name for name in unresolved)


def test_control_a_name_found_nowhere_fills_nothing(tmp_path: Path) -> None:
    """The failing control: with the existing name absent from the file and the store the variable stays unfilled."""
    filled, unresolved = fill_declared_settings(
        {}, OVERLAY, home=_home(tmp_path, "UNRELATED=x\n"), store=FakeStore({})
    )

    assert unresolved == ("RUN_KEY", "RUN_PAIR")
    assert "RUN_KEY" not in filled
    assert "RUN_PAIR" not in filled
