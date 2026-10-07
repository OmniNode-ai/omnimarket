# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OmniDash static bundle: pinned, fetched once, verified before it is served.

Decision D8 was ruled (a) on 2026-09-30, firm: a developer runs the dashboard
with no clone and no Node, so ``onex dashboard`` downloads a prebuilt copy of the
pages and verifies it. omnidash publishes that copy as a release asset with its
sha256 (omnidash's ``publish-dashboard-bundle.yml``).

THE PIN IS THE SECURITY BOUNDARY, AND IT LIVES IN THIS REPOSITORY. The digest
compared against is the one in our own ``pyproject.toml``, never the ``.sha256``
file published beside the asset: anyone who can replace the bundle can replace
that file too, so trusting it would make the verification decorative. The asset
is fetched over HTTPS from a public release, which authenticates the host and
nothing about the bytes.

WHY THE PIN SHIPS INSIDE THE PACKAGE. ``pyproject.toml`` is not in the wheel, so
a pin read from the source tree would be unreadable exactly where it matters --
on an installed copy with no checkout, which is the only configuration D8
describes. Rather than keep a second copy in a Python constant and hope the two
stay equal, the build force-includes ``pyproject.toml`` into the package (the
same mechanism the repository already uses for ``config/ci_bus_lanes.yaml``), so
there is one source of truth and it is the file a human edits.

A TAMPERED OR TRUNCATED CACHE IS REFUSED, NOT REPAIRED. The digest is checked
against the bytes on every start, not only on the download, because the
interesting case is a cache that was correct when written and is not now. On a
mismatch the extracted copy is left alone and the caller is refused: a cache
this code silently re-downloaded would turn an attack into a cache miss and
report nothing.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: Where a verified bundle is unpacked. Keyed by digest, so a pin bump lands
#: beside the old copy rather than over it and a rollback needs no re-download.
_CACHE_ROOT = Path.home() / ".omninode" / "dashboard" / "bundles"

#: Read in 1 MiB blocks: the bundle is a few megabytes and a whole-file read
#: would hold it twice in memory for no gain.
_BLOCK = 1024 * 1024


class DashboardBundleError(RuntimeError):
    """The bundle could not be made available, with the reason a person needs."""


@dataclass(frozen=True)
class BundlePin:
    """What the installed copy says it must serve."""

    version: str
    sha256: str
    asset: str
    repository: str

    @property
    def url(self) -> str:
        return (
            f"https://github.com/{self.repository}/releases/download/"
            f"{self.version}/{self.asset}"
        )


def _pin_path() -> Path:
    """The packaged copy of ``pyproject.toml``, or the source tree's in a checkout."""
    packaged = Path(__file__).resolve().parents[2] / "dashboard_bundle_pin.toml"
    if packaged.is_file():
        return packaged
    # A checkout, where src/omnimarket/../.. is the repository root. Four parents
    # from this file: handlers' sibling -> node -> nodes -> omnimarket -> src.
    for candidate in (
        Path(__file__).resolve().parents[4] / "pyproject.toml",
        Path(__file__).resolve().parents[3] / "pyproject.toml",
    ):
        if candidate.is_file():
            return candidate
    raise DashboardBundleError(
        "no dashboard bundle pin is readable: neither the packaged "
        "dashboard_bundle_pin.toml nor a pyproject.toml above this module"
    )


def load_pin(path: Path | None = None) -> BundlePin:
    """The pinned bundle, from the one file a human edits."""
    source = path or _pin_path()
    try:
        table = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DashboardBundleError(f"{source} is unreadable: {exc}") from exc
    pin = (table.get("tool") or {}).get("onex", {}).get("dashboard_bundle") or {}
    missing = [k for k in ("version", "sha256", "asset", "repository") if not pin.get(k)]
    if missing:
        raise DashboardBundleError(
            f"[tool.onex.dashboard_bundle] in {source} is missing {', '.join(missing)}"
        )
    digest = str(pin["sha256"]).strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise DashboardBundleError(
            f"[tool.onex.dashboard_bundle].sha256 is not a sha256 digest: {pin['sha256']!r}"
        )
    return BundlePin(
        version=str(pin["version"]),
        sha256=digest,
        asset=str(pin["asset"]),
        repository=str(pin["repository"]),
    )


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_BLOCK):
            h.update(chunk)
    return h.hexdigest()


def _extract(archive: Path, into: Path) -> None:
    """Unpack, refusing any member that would land outside ``into``.

    A release asset is not a trusted archive just because its digest matched the
    pin: the pin says these are the bytes we chose, not that the bytes are kind.
    A member named ``../../.ssh/authorized_keys`` matches its digest perfectly.
    """
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        root = into.resolve()
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if not target.is_relative_to(root):
                raise DashboardBundleError(
                    f"refusing the bundle: member {member.name!r} escapes the "
                    "extraction directory"
                )
            if member.issym() or member.islnk():
                raise DashboardBundleError(
                    f"refusing the bundle: member {member.name!r} is a link"
                )
        # ``filter="data"`` is a second, independent refusal of the same
        # classes the loop above names, kept because tarfile's own check is
        # maintained against archive tricks this code has not thought of.
        tar.extractall(into, filter="data")  # noqa: S202 - filtered and checked


def _download(url: str, to: Path) -> None:
    to.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - https release URL built from the pin
            to.write_bytes(response.read())
    except (urllib.error.URLError, OSError) as exc:
        raise DashboardBundleError(
            f"could not download the dashboard bundle from {url}: {exc}"
        ) from exc


def ensure_bundle(
    pin: BundlePin | None = None,
    *,
    cache_root: Path | None = None,
    allow_download: bool = True,
) -> Path:
    """The directory holding the verified pages, fetching it once if needed.

    Raises :class:`DashboardBundleError` with a sentence a developer can act on
    when the bundle is absent and cannot be fetched, or present and wrong.
    """
    pin = pin or load_pin()
    root = (cache_root or _CACHE_ROOT) / pin.sha256
    marker = root / "index.html"
    archive = root.with_suffix(".tar.gz")

    if marker.is_file() and archive.is_file():
        # Re-verify on every start: the case worth catching is a cache that was
        # right when it was written and is not right now.
        found = _digest(archive)
        if found != pin.sha256:
            raise DashboardBundleError(
                f"the cached dashboard bundle does not match the pin.\n"
                f"  pinned: {pin.sha256}\n"
                f"  found:  {found}\n"
                f"  at:     {archive}\n"
                "Nothing was re-downloaded and nothing was served. Delete that "
                "file to fetch it again, after satisfying yourself about why it "
                "changed."
            )
        return root

    if not allow_download:
        raise DashboardBundleError(
            f"the dashboard bundle {pin.asset} is not cached and downloading is "
            "disabled"
        )

    _download(pin.url, archive)
    found = _digest(archive)
    if found != pin.sha256:
        archive.unlink(missing_ok=True)
        raise DashboardBundleError(
            f"the downloaded dashboard bundle does not match the pin, so it was "
            f"discarded and nothing was served.\n"
            f"  pinned: {pin.sha256}\n"
            f"  found:  {found}\n"
            f"  from:   {pin.url}"
        )
    if root.exists():
        shutil.rmtree(root)
    _extract(archive, root)
    if not marker.is_file():
        raise DashboardBundleError(
            f"the bundle {pin.asset} carries no index.html, so there is no page "
            "to serve"
        )
    return root


__all__ = [
    "BundlePin",
    "DashboardBundleError",
    "ensure_bundle",
    "load_pin",
]
