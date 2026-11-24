"""The overlay reader consumes the omnibase_spi store protocol and defines no second one."""

from __future__ import annotations

from pathlib import Path

import omnimarket.handlers.goal_scope_overlay as goal_scope_overlay

_SOURCE = Path(goal_scope_overlay.__file__).read_text(encoding="utf-8")


def test_reader_imports_the_spi_overlay_store_protocol() -> None:
    assert "from omnibase_spi.protocols.storage import ProtocolOverlayStore" in _SOURCE


def test_reader_declares_no_protocol_class_of_its_own() -> None:
    assert "(Protocol)" not in _SOURCE
