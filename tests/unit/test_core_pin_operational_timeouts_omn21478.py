# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed core pin reads its operational timeouts from a packaged contract.

omnibase_core moved its operational budgets out of code literals into a packaged
``operational_timeouts.yaml`` read by ``ModelOperationalTimeouts``. A core pin older
than that change has no such module, so these tests fail against it.
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_MODULE = "omnibase_core.models.runtime.model_operational_timeouts"


def test_installed_core_loads_every_packaged_budget_as_positive_seconds() -> None:
    module = importlib.import_module(_MODULE)

    timeouts = module.packaged_timeouts()
    fields = type(timeouts).model_fields

    assert isinstance(timeouts, module.ModelOperationalTimeouts)
    assert module.ModelOperationalTimeouts.packaged() is timeouts
    assert fields, "the packaged contract declares no budgets"
    assert all(getattr(timeouts, name) > 0 for name in fields)


def test_installed_core_refuses_an_unknown_or_missing_budget() -> None:
    module = importlib.import_module(_MODULE)
    declared = module.packaged_timeouts().model_dump()

    with pytest.raises(ValidationError):
        module.ModelOperationalTimeouts(**declared, undeclared_seconds=1.0)

    missing = dict(declared)
    missing.pop(next(iter(missing)))
    with pytest.raises(ValidationError):
        module.ModelOperationalTimeouts(**missing)
