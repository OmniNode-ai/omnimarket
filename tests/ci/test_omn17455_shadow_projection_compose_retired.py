"""The retired, unmanaged projection compose lane must not return (OMN-17455)."""

from pathlib import Path


def test_ungoverned_projection_compose_file_is_absent() -> None:
    """Registration must run only under the governed dev-lane compose project."""
    repo_root = Path(__file__).resolve().parents[2]
    assert not (repo_root / "docker-compose.projection.yml").exists()
