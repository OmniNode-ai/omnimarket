# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for JsonFileTwoStrikeStore (OMN-13940).

Persistence matters here: merge-sweep runs as a fresh process per tick, so an
in-memory counter would silently reset every tick and never trip. These
tests prove the counter survives a fresh store instance pointed at the same
state dir (simulating a new process/tick).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_two_strike_store import (
    JsonFileTwoStrikeStore,
    strike_key,
)


@pytest.mark.unit
class TestStrikeKey:
    def test_key_is_deterministic_and_scoped(self) -> None:
        key = strike_key("OmniNode-ai/omnimarket", 42, "code_failure")
        assert key == "OmniNode-ai/omnimarket#42:code_failure"

    def test_different_block_reason_different_key(self) -> None:
        k1 = strike_key("OmniNode-ai/omnimarket", 42, "code_failure")
        k2 = strike_key("OmniNode-ai/omnimarket", 42, "changes_requested")
        assert k1 != k2


@pytest.mark.unit
class TestJsonFileTwoStrikeStore:
    def test_get_strikes_defaults_to_zero(self, tmp_path: Path) -> None:
        store = JsonFileTwoStrikeStore(state_dir=tmp_path)
        assert store.get_strikes("some-key") == 0

    def test_record_failure_increments_and_persists(self, tmp_path: Path) -> None:
        store = JsonFileTwoStrikeStore(state_dir=tmp_path)
        assert store.record_failure("k1") == 1
        assert store.record_failure("k1") == 2
        assert store.get_strikes("k1") == 2

    def test_persists_across_fresh_store_instances(self, tmp_path: Path) -> None:
        """Simulates a new merge-sweep process/tick reading the same state dir."""
        store_a = JsonFileTwoStrikeStore(state_dir=tmp_path)
        store_a.record_failure("k1")
        store_a.record_failure("k1")

        store_b = JsonFileTwoStrikeStore(state_dir=tmp_path)
        assert store_b.get_strikes("k1") == 2

    def test_independent_keys_do_not_interfere(self, tmp_path: Path) -> None:
        store = JsonFileTwoStrikeStore(state_dir=tmp_path)
        store.record_failure("k1")
        assert store.get_strikes("k1") == 1
        assert store.get_strikes("k2") == 0

    def test_resolve_state_dir_prefers_onex_state_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.delenv("OMNI_HOME", raising=False)
        store = JsonFileTwoStrikeStore()
        store.record_failure("k1")
        assert (tmp_path / "state" / "delegated_fix" / "two_strike.json").exists()

    def test_resolve_state_dir_falls_back_to_omni_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        store = JsonFileTwoStrikeStore()
        store.record_failure("k1")
        assert (tmp_path / ".onex_state" / "delegated_fix" / "two_strike.json").exists()

    def test_resolve_state_dir_raises_when_neither_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
        monkeypatch.delenv("OMNI_HOME", raising=False)
        with pytest.raises(RuntimeError, match="ONEX_STATE_DIR or OMNI_HOME"):
            JsonFileTwoStrikeStore()
