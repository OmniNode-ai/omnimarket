"""The declared interpreter bound must admit the interpreter the customer guide names.

OMN-17928. `requires-python` was `>=3.12,<3.13` from repo initialisation with no comment
and no ticket, while the canonical customer interpreter is CPython 3.13. As declared, the
published package was uninstallable by `pip` on 3.13 — and `uv tool install` placed it into
a 3.13 environment the metadata excludes, so the two tools disagreed and the documented
install recipe was not reproducible.

Nothing exercised the bound, which is why an unjustified cap survived from 2026-04-04. This
is that missing check: the bound is a claim about which interpreters the package supports,
and a claim with no executing test behind it is how this defect lasted five months.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

#: The interpreter the customer guides and shared standards name (OMN-17928).
CANONICAL_CUSTOMER_INTERPRETER = "3.13.11"
#: The declared floor. Kept explicit so a change to either end fails loudly.
SUPPORTED_FLOOR = "3.12.0"
BELOW_FLOOR = "3.11.9"

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_bound_admits_the_canonical_customer_interpreter(project: dict) -> None:
    """3.13 must install. This is the assertion the ticket exists for."""
    spec = SpecifierSet(project["requires-python"])
    assert CANONICAL_CUSTOMER_INTERPRETER in spec, (
        f"requires-python={project['requires-python']!r} excludes the canonical customer "
        f"interpreter {CANONICAL_CUSTOMER_INTERPRETER}. A customer following the guide "
        f"cannot install this package."
    )


def test_bound_still_floors_at_the_supported_minimum(project: dict) -> None:
    """Admitting 3.13 must not silently drop the floor — a bound that admits everything
    is as untrue as one that admits too little."""
    spec = SpecifierSet(project["requires-python"])
    assert SUPPORTED_FLOOR in spec
    assert BELOW_FLOOR not in spec, (
        f"requires-python={project['requires-python']!r} admits {BELOW_FLOOR}, below the "
        f"declared 3.12 floor."
    )


def test_classifiers_do_not_understate_the_supported_interpreters(
    project: dict,
) -> None:
    """The trove classifiers are read by humans and by PyPI's UI. If the bound admits an
    interpreter the classifiers omit, the package's own metadata disagrees with itself."""
    spec = SpecifierSet(project["requires-python"])
    classifiers = set(project.get("classifiers", []))
    for minor in ("3.12", "3.13"):
        if f"{minor}.0" in spec:
            assert f"Programming Language :: Python :: {minor}" in classifiers, (
                f"requires-python admits {minor} but no trove classifier declares it."
            )
