# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1/AC3: a store kind takes its connector as an argument, so no connection is made by the backend itself."""

from __future__ import annotations

from typing import Any

from omnimarket.handlers.overlay_store import backend_postgres as pg
from omnimarket.handlers.overlay_store.backend_valkey import (
    ADDRESS_INPUT,
    DATABASE_INPUT,
    KEY_PREFIX_INPUT,
    LOGIN_INPUT,
    PASSWORD_SECRET,
    PORT_INPUT,
    BackendValkey,
)


def test_backend_connector_injection_valkey_hands_the_wiring_values_to_the_injected_connector() -> (
    None
):
    seen: list[dict[str, Any]] = []

    def connector(**kwargs: Any) -> object:
        seen.append(kwargs)
        return object()

    inputs = {
        ADDRESS_INPUT: "a-host",
        PORT_INPUT: "1234",
        DATABASE_INPUT: "2",
        KEY_PREFIX_INPUT: "p:",
        LOGIN_INPUT: "a-login",
    }
    BackendValkey(connector).build(inputs, {PASSWORD_SECRET: "a-secret"})
    assert seen == [
        {
            "host": "a-host",
            "port": 1234,
            "database": 2,
            "username": "a-login",
            "password": "a-secret",
        }
    ]


async def test_backend_connector_injection_postgres_opens_nothing_until_first_use_and_then_through_the_injected_connector() -> (
    None
):
    opened: list[dict[str, Any]] = []

    class _Pool:
        def acquire(self) -> object:
            return object()

    async def connector(**kwargs: Any) -> _Pool:
        opened.append(kwargs)
        return _Pool()

    inputs = {
        pg.ADDRESS_INPUT: "a-host",
        pg.PORT_INPUT: "5",
        pg.DATABASE_INPUT: "d",
        pg.LOGIN_INPUT: "u",
    }
    store = pg.BackendPostgres(connector).build(inputs, {pg.PASSWORD_SECRET: "s"})
    assert opened == []
    assert store is not None
