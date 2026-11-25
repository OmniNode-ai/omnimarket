# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from tests.helpers.fake_alarm_ledger import install_alarm_ledger


@pytest.fixture(autouse=True)
def alarm_ledger_on_file(monkeypatch: pytest.MonkeyPatch) -> None:
    install_alarm_ledger(monkeypatch)
