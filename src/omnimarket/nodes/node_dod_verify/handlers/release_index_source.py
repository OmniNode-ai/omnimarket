# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Release-index I/O used by the existing DoD evidence effect handler."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Final

from omnimarket.config.service_endpoints import service_endpoint_url

logger = logging.getLogger(__name__)
_DEFAULT_INDEX_TIMEOUT_S: Final[float] = 20.0
_PYPI: Final[str] = service_endpoint_url("pypi.base_url")
PACKAGE_INDEX_JSON_URL: Final[str] = f"{_PYPI}/pypi/{{distribution}}/{{version}}/json"


def pypi_release_files(
    distribution: str,
    version: str,
    *,
    timeout_s: float = _DEFAULT_INDEX_TIMEOUT_S,
) -> frozenset[str] | None:
    """Read the distribution file types the index serves for one version.

    ``frozenset()`` on a definite 404 (the index does not have that version).
    Retry a transport failure once; exhausted retries and other failures return
    ``None``, which is INDETERMINATE and never certifies.
    """
    url = PACKAGE_INDEX_JSON_URL.format(distribution=distribution, version=version)
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as response:
                if response.status != 200:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
            urls = payload.get("urls")
            if not isinstance(urls, list):
                return None
            types = {
                entry["packagetype"]
                for entry in urls
                if isinstance(entry, dict) and isinstance(entry.get("packagetype"), str)
            }
            return frozenset(types)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                logger.warning("Release-index read returned HTTP %d", exc.code)
            return frozenset() if exc.code == 404 else None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning(
                "Release-index transport attempt %d/2 failed (%s)",
                attempt + 1,
                type(exc).__name__,
            )
        except json.JSONDecodeError:
            return None
    return None
