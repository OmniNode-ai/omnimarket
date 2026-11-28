# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (Task 20), rules half: the three logins' access-list rules, rendered from the wiring's prefix and login names."""

from __future__ import annotations

from omnimarket.handlers.overlay_store.valkey_acl import (
    ModelValkeyStoreLogins,
    render_store_acl_commands,
)

LOGINS = ModelValkeyStoreLogins(
    key_prefix="px",
    writer_login="w-login",
    reader_login="r-login",
    cache_login="c-login",
    cache_key_pattern="cx:*",
)


def _rules(login: str) -> list[str]:
    commands = render_store_acl_commands(
        LOGINS, passwords={"w-login": "w", "r-login": "r", "c-login": "c"}
    )
    (rule,) = [c for c in commands if c[2] == login]
    return list(rule[3:])


def test_valkey_acl_rules_reader_can_only_read_the_store_prefix() -> None:
    rule = " ".join(_rules("r-login"))
    assert "%R~px:*" in rule
    assert "%RW~px:*" not in rule
    assert "%W~px:*" not in rule
    assert "~*" not in rule.replace("%R~px:*", "")


def test_valkey_acl_rules_writer_reads_and_writes_the_store_prefix_only() -> None:
    rule = " ".join(_rules("w-login"))
    assert "%RW~px:*" in rule
    assert "~*" not in rule.replace("%RW~px:*", "")


def test_valkey_acl_rules_cache_login_has_no_access_to_the_store_prefix() -> None:
    rule = " ".join(_rules("c-login"))
    assert "%R~px:*" not in rule
    assert "%RW~px:*" not in rule
    assert "%W~px:*" not in rule
    assert "-@all" in rule or "resetkeys" in rule
    # the narrowed cache login names its own keys, never every key
    assert "allkeys" not in rule
    assert " ~*" not in f" {rule}"


def test_valkey_acl_rules_every_login_starts_from_nothing() -> None:
    for login in ("w-login", "r-login", "c-login"):
        rules = _rules(login)
        assert rules[0] == "reset"


def test_valkey_acl_rules_every_login_may_select_its_database() -> None:
    # The store's client connects with a database number, so it runs SELECT first; without +select the login
    # authenticates and then every command is refused (measured on the dev Valkey, OMN-24052).
    for login in ("w-login", "r-login", "c-login"):
        assert "+select" in _rules(login)
