"""Contract test (OMN-13836): omnimarket must stay resolvable as a declared dep.

OMN-13829 proved omnimarket could not be declared as a real dependency of
omnibase_infra because the ``[tool.uv] override-dependencies`` git-pinned
``omnibase-core`` / ``omnibase-infra`` / ``omnibase-compat`` to foreign revs.
A git-URL override forces a URL requirement that clashes with a consumer's own
core/infra pins ("conflicting-URL / broken-lock"), so any reintroduction of a
git-URL override is a regression.

These tests pin the omnimarket-side invariants from OMN-13836:

1. No ``[tool.uv] override-dependencies`` entry may use a git URL.
2. The direct ``omnibase-core`` / ``omnibase-spi`` constraints must sit on the
   current resolvable ranges (``core>=0.46.7``, ``spi>=0.23.0``), consistent
   with OMN-14168 and omnibase_infra @main. The floors are asserted as
   MINIMUMS, compared as parsed versions (OMN-15539): raising a floor onto a
   newer published release is the intended way to retire a git-rev override,
   and a substring check on the old literal would fail that legitimate move
   while still passing for a lower, actually-regressed floor like ``>=0.46.70``.

They do NOT assert full cross-repo resolvability: the residual blocker is
``omninode-memory==0.15.0`` (hard-pins ``omnibase-spi==0.20.6``), which lives
in the omnimemory repo and is out of scope for this ticket.
"""

import re
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

_LOWER_BOUND = re.compile(r">=\s*(\d+)\.(\d+)\.(\d+)")


def _lower_bound(requirement: str) -> tuple[int, int, int]:
    """Parse the ``>=`` floor of a PEP 508 requirement as a comparable tuple."""
    match = _LOWER_BOUND.search(requirement)
    assert match is not None, (
        f"requirement must declare a >= lower bound, got: {requirement!r}"
    )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _load() -> dict:
    with _PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def test_no_git_url_override_dependencies() -> None:
    """No [tool.uv] override-dependencies entry may pin to a git URL."""
    data = _load()
    overrides = data.get("tool", {}).get("uv", {}).get("override-dependencies", [])

    git_overrides = [o for o in overrides if "git+" in o or "@ git" in o]

    assert not git_overrides, (
        "OMN-13836: [tool.uv] override-dependencies must not git-pin to foreign "
        "revs (conflicting-URL when omnimarket is consumed as a dependency). "
        f"Offending entries: {git_overrides!r}"
    )


def test_no_git_url_override_for_onex_packages() -> None:
    """Belt-and-suspenders: the ONEX packages specifically must be range-pinned."""
    data = _load()
    overrides = data.get("tool", {}).get("uv", {}).get("override-dependencies", [])

    onex_pkgs = ("omnibase-core", "omnibase-spi", "omnibase-infra", "omnibase-compat")
    for override in overrides:
        for pkg in onex_pkgs:
            if not override.startswith(pkg):
                continue
            assert "git+" not in override, (
                f"OMN-13836: override for {pkg} must be a version range, "
                f"not a git URL. Got: {override!r}"
            )
            assert "@ git" not in override, (
                f"OMN-13836: override for {pkg} must be a version range, "
                f"not a git URL. Got: {override!r}"
            )


def test_core_and_spi_direct_constraints_on_resolvable_ranges() -> None:
    """Direct core/spi constraints must be on the current resolvable ranges."""
    data = _load()
    deps = data.get("project", {}).get("dependencies", [])

    core = next((d for d in deps if d.startswith("omnibase-core")), None)
    spi = next((d for d in deps if d.startswith("omnibase-spi")), None)

    assert core is not None, "omnibase-core missing from project.dependencies"
    assert spi is not None, "omnibase-spi missing from project.dependencies"

    # OMN-14168 requires core>=0.46.7; OMN-13836 still forbids git URL pins.
    # Compared as parsed versions so a floor RAISE (the sanctioned way to drop a
    # git-rev override once the feature publishes) passes while a floor DROP
    # still fails -- OMN-15539 raised core to >=0.46.8 for the delegation wire
    # contracts and a substring check on ">=0.46.7" rejected it.
    assert _lower_bound(core) >= (0, 46, 7), (
        f"omnibase-core must require >=0.46.7 or newer (OMN-14168). Got: {core!r}"
    )
    assert _lower_bound(spi) >= (0, 23, 0), (
        f"omnibase-spi must require >=0.23.0 or newer (OMN-13836). Got: {spi!r}"
    )
