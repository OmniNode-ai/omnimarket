# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The key hand-off the runtime route uses: a parked key is taken once, never recorded.

The CLI runs the setup workflow in its own process, so no command carries the key; the
node handler still accepts a ``key_handoff`` token when a runtime delivers one.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    discard_key,
    park_key,
    take_key,
)

pytestmark = pytest.mark.unit


def test_a_parked_key_is_taken_once_and_an_unclaimed_one_is_discarded() -> None:
    token = park_key(SecretStr("sk-parked"))
    taken = take_key(token)
    assert taken is not None
    assert taken.get_secret_value() == "sk-parked"
    assert take_key(token) is None
    unclaimed = park_key(SecretStr("sk-unclaimed"))
    discard_key(unclaimed)
    assert take_key(unclaimed) is None
    assert take_key(None) is None
