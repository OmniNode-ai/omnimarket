# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (Task 20), live half, against the dev Valkey on the lab (collected only when ONEX_VALKEY_ACL_PROBE is set).

Every connection value comes from the environment of the lab run; nothing here names an address, port or prefix.
"""

from __future__ import annotations

import os

import pytest
import redis
from redis.exceptions import NoPermissionError


def _client(login_var: str, password_var: str) -> redis.Redis:
    return redis.Redis(
        host=os.environ["ONEX_VALKEY_PROBE_ADDRESS"],
        port=int(os.environ["ONEX_VALKEY_PROBE_PORT"]),
        db=int(os.environ["ONEX_VALKEY_PROBE_DATABASE"]),
        username=os.environ[login_var],
        password=os.environ[password_var],
        decode_responses=True,
    )


PREFIX = os.environ.get("ONEX_VALKEY_PROBE_KEY_PREFIX", "")


def test_valkey_acl_refusals_the_cache_login_cannot_read_a_populated_settings_key() -> (
    None
):
    populated = os.environ["ONEX_VALKEY_PROBE_POPULATED_KEY"]
    cache = _client("ONEX_VALKEY_PROBE_CACHE_LOGIN", "ONEX_VALKEY_PROBE_CACHE_PASSWORD")
    with pytest.raises(NoPermissionError):
        cache.get(populated)


def test_valkey_acl_refusals_the_reader_login_cannot_write_the_store_prefix() -> None:
    reader = _client(
        "ONEX_VALKEY_PROBE_READER_LOGIN", "ONEX_VALKEY_PROBE_READER_PASSWORD"
    )
    with pytest.raises(NoPermissionError):
        reader.set(f"{PREFIX}:acl-probe", "x")


def test_valkey_acl_refusals_the_cache_login_cannot_write_the_store_prefix() -> None:
    cache = _client("ONEX_VALKEY_PROBE_CACHE_LOGIN", "ONEX_VALKEY_PROBE_CACHE_PASSWORD")
    with pytest.raises(NoPermissionError):
        cache.set(f"{PREFIX}:acl-probe", "x")


def test_valkey_acl_refusals_positive_control_the_reader_login_reads_the_populated_key() -> (
    None
):
    reader = _client(
        "ONEX_VALKEY_PROBE_READER_LOGIN", "ONEX_VALKEY_PROBE_READER_PASSWORD"
    )
    assert reader.get(os.environ["ONEX_VALKEY_PROBE_POPULATED_KEY"]) is not None
