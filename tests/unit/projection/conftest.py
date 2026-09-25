"""Module-scoped fixtures for tests/unit/projection (OMN-19680 A4).

``build_projection_topic_map()`` called with no manifest triggers a live
``discover_contracts()`` filesystem scan of every ``contract.yaml`` under
``src/omnimarket/nodes/`` (423 directories as of this change). Ten cases in
``test_projection_discovery.py`` call it with no arguments to exercise the
real, undoctored catalog rather than a stubbed manifest; each one re-ran that
same scan from scratch, at roughly 10.5s per call in CI (105.9s summed over
the file, run 36186846950).

The scan is a pure read (``yaml.safe_load`` over files already on disk) with
no mutation and no external state, so its result is safe to compute once per
test module and share across cases. This fixture does exactly that; the ten
call sites in ``test_projection_discovery.py`` take it as a parameter instead
of calling ``build_projection_topic_map()`` directly.
"""

from __future__ import annotations

import pytest

from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig


@pytest.fixture(scope="module")
def real_topic_map() -> dict[str, ProjectionTableConfig]:
    """The live, undoctored topic map, built once per test module."""
    return build_projection_topic_map()
