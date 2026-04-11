"""Contract test: all onex.nodes entry points must use package form (no colon)."""

import tomllib
from pathlib import Path


def test_all_node_entry_points_are_package_form() -> None:
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)

    entry_points = data.get("project", {}).get("entry-points", {}).get("onex.nodes", {})
    assert entry_points, "No onex.nodes entry points found in pyproject.toml"

    violations = {name: value for name, value in entry_points.items() if ":" in value}

    assert not violations, (
        f"{len(violations)} entry point(s) use class form (colon) instead of package form:\n"
        + "\n".join(f"  {k} = {v!r}" for k, v in sorted(violations.items()))
    )
