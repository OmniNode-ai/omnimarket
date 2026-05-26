"""NodeDashboardSweep — Dashboard page classification and triage.

Classifies dashboard pages as HEALTHY, EMPTY, MOCK, BROKEN, or FLAG_GATED,
then groups them into problem domains and assigns fix tiers (CODE_BUG,
DATA_PIPELINE, SCHEMA_MISMATCH, FEATURE_GAP, FLAG_GATE).

Phase 1 (optional): HTTP recon — discover routes and collect raw page metadata
from a live dashboard URL. Activated when ``base_url`` is provided.

Phase 2: Classification — map raw HTTP metadata (or pre-supplied page inputs)
to ONEX semantic status using the standard decision tree.

Phase 3: Triage — group classified pages into problem domains with fix tiers.

ONEX node type: EFFECT when base_url is provided (performs HTTP I/O);
COMPUTE when only pre-classified ``pages`` are supplied.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from enum import StrEnum
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

_RECON_TIMEOUT_S = 10
_RECON_BODY_LIMIT = 2000
_ERROR_INDICATORS = (
    "Error:",
    "500 Internal Server Error",
    "Application error",
    "ChunkLoadError",
    "Failed to fetch",
)

_KNOWN_ROUTES = (
    "/",
    "/agents",
    "/events",
    "/metrics",
    "/settings",
    "/intelligence",
    "/delegation",
    "/api/build-info",
)


class EnumPageStatus(StrEnum):
    """Dashboard page classification status."""

    HEALTHY = "HEALTHY"
    EMPTY = "EMPTY"
    MOCK = "MOCK"
    BROKEN = "BROKEN"
    FLAG_GATED = "FLAG_GATED"


class EnumFixTier(StrEnum):
    """Fix tier for a problem domain."""

    CODE_BUG = "CODE_BUG"
    DATA_PIPELINE = "DATA_PIPELINE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    FEATURE_GAP = "FEATURE_GAP"
    FLAG_GATE = "FLAG_GATE"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelPageInput(BaseModel):
    """Input for a single dashboard page (semantic/pre-classified form)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str
    has_data: bool = False
    has_live_timestamps: bool = False
    has_js_errors: bool = False
    has_network_errors: bool = False
    has_mock_patterns: bool = False
    has_feature_flag: bool = False
    visible_text: str = ""


class ModelReconResult(BaseModel):
    """Raw HTTP recon result for a single dashboard route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str
    status_code: int
    content_type: str
    body_size: int
    body_snippet: str
    has_error_text: bool


class ModelPageStatus(BaseModel):
    """Classification result for a single page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str
    status: EnumPageStatus
    reason: str


class ModelProblemDomain(BaseModel):
    """A grouped problem domain with fix tier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain_id: str
    pages: list[str]
    fix_tier: EnumFixTier
    hypothesis: str


class DashboardSweepRequest(BaseModel):
    """Input for the dashboard sweep handler.

    Two modes:
    - ``base_url`` set: Phase 1 HTTP recon runs first, discovers routes,
      collects raw page metadata, then proceeds to classification.
    - ``pages`` only: skip HTTP recon; classify the supplied pre-analysed pages.

    Both modes are fully supported and backward-compatible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Recon mode — mutually exclusive with pre-supplied pages (but both may
    # be provided; pre-supplied pages are merged with recon discoveries).
    base_url: str | None = Field(
        default=None,
        description="Dashboard base URL for HTTP recon (e.g. http://localhost:3000). "
        "When set, the handler performs Phase 1 HTTP recon before classification.",
    )
    extra_routes: list[str] = Field(
        default_factory=list,
        description="Additional routes to probe during HTTP recon (beyond auto-discovered).",
    )

    # Pre-classified mode (legacy / pass-through).
    pages: list[ModelPageInput] = Field(default_factory=list)

    max_iterations: int = 3
    dry_run: bool = False


class DashboardSweepResult(BaseModel):
    """Output of the dashboard sweep handler."""

    model_config = ConfigDict(extra="forbid")

    page_statuses: list[ModelPageStatus] = Field(default_factory=list)
    domains: list[ModelProblemDomain] = Field(default_factory=list)
    recon_results: list[ModelReconResult] = Field(default_factory=list)
    pages_total: int = 0
    status: str = "clean"  # clean | issues_found | error
    dry_run: bool = False

    @property
    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for ps in self.page_statuses:
            counts[ps.status] = counts.get(ps.status, 0) + 1
        return counts

    @property
    def by_fix_tier(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.domains:
            counts[d.fix_tier] = counts.get(d.fix_tier, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MOCK_PATTERNS = [
    re.compile(r"Sample\s+Agent", re.IGNORECASE),
    re.compile(r"lorem\s+ipsum", re.IGNORECASE),
    re.compile(r"count:\s*42\b"),
    re.compile(r"placeholder", re.IGNORECASE),
    re.compile(r"example\.com"),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _LinkExtractor(HTMLParser):
    """Extract same-origin href values from HTML."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.routes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self._process_href(value)

    def _process_href(self, href: str) -> None:
        # Keep only same-origin paths.
        if href.startswith("/") and not href.startswith("//"):
            route = href.split("?")[0].split("#")[0]
            if route and route not in self.routes:
                self.routes.append(route)
        elif href.startswith(self.base_url):
            path = href[len(self.base_url) :]
            route = path.split("?")[0].split("#")[0] or "/"
            if route not in self.routes:
                self.routes.append(route)


def _fetch_page(url: str) -> tuple[int, str, int, str]:
    """Fetch a URL and return (status_code, content_type, body_size, body_snippet).

    Falls back to (0, '', 0, '') on any network or timeout error.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "onex-dashboard-sweep/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_RECON_TIMEOUT_S) as resp:
            status_code: int = resp.status
            content_type: str = resp.headers.get("Content-Type", "")
            body_bytes: bytes = resp.read(_RECON_BODY_LIMIT)
            body_size: int = int(resp.headers.get("Content-Length", len(body_bytes)))
            body_snippet: str = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        return 0, "", 0, ""
    return status_code, content_type, body_size, body_snippet


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeDashboardSweep:
    """Classify dashboard pages and triage into problem domains.

    Operates in two modes:
    1. HTTP Recon mode — when ``request.base_url`` is set, Phase 1 performs
       live HTTP recon to discover routes and collect raw page metadata.
    2. Pass-through mode — classifies pre-supplied ``request.pages`` directly.
    """

    def handle(self, request: DashboardSweepRequest) -> DashboardSweepResult:
        """Execute the dashboard sweep across page inputs."""
        recon_results: list[ModelReconResult] = []

        if request.base_url:
            recon_results = self._run_recon(request.base_url, request.extra_routes)
            recon_pages = [self._recon_to_page_input(r) for r in recon_results]
            # Merge with any pre-supplied pages (pre-supplied take priority by route).
            existing_routes = {p.route for p in request.pages}
            merged = list(request.pages) + [
                p for p in recon_pages if p.route not in existing_routes
            ]
        else:
            merged = list(request.pages)

        page_statuses: list[ModelPageStatus] = []
        broken_pages: list[ModelPageStatus] = []

        for page in merged:
            status = self._classify_page(page)
            page_statuses.append(status)
            if status.status in (EnumPageStatus.BROKEN, EnumPageStatus.EMPTY):
                broken_pages.append(status)

        domains = self._triage_domains(broken_pages, merged)

        overall = "clean" if not broken_pages else "issues_found"

        return DashboardSweepResult(
            page_statuses=page_statuses,
            domains=domains,
            recon_results=recon_results,
            pages_total=len(merged),
            status=overall,
            dry_run=request.dry_run,
        )

    # ------------------------------------------------------------------
    # Phase 1 — HTTP Recon
    # ------------------------------------------------------------------

    def _run_recon(
        self, base_url: str, extra_routes: list[str]
    ) -> list[ModelReconResult]:
        """Probe base_url to discover routes, then recon each route."""
        base_url = base_url.rstrip("/")

        # Discover routes from the home page HTML.
        _status, _ct, _size, home_html = _fetch_page(base_url + "/")
        extractor = _LinkExtractor(base_url)
        extractor.feed(home_html)
        discovered = extractor.routes

        # Union of discovered + known + caller-supplied extra routes.
        all_routes: list[str] = []
        seen: set[str] = set()
        for route in list(discovered) + list(_KNOWN_ROUTES) + list(extra_routes):
            if route not in seen:
                seen.add(route)
                all_routes.append(route)

        results: list[ModelReconResult] = []
        for route in all_routes:
            url = base_url + route
            status_code, content_type, body_size, body_snippet = _fetch_page(url)
            has_error_text = any(ind in body_snippet for ind in _ERROR_INDICATORS)
            results.append(
                ModelReconResult(
                    route=route,
                    status_code=status_code,
                    content_type=content_type,
                    body_size=body_size,
                    body_snippet=body_snippet,
                    has_error_text=has_error_text,
                )
            )

        return results

    def _recon_to_page_input(self, recon: ModelReconResult) -> ModelPageInput:
        """Convert a raw recon result into a semantic ModelPageInput.

        Heuristics:
        - status_code 0 or 5xx → has_network_errors
        - has_error_text → has_js_errors
        - status_code 200 + content-type text/html → check body for mock patterns
        """
        has_network_errors = recon.status_code == 0 or recon.status_code >= 500
        has_js_errors = recon.has_error_text and not has_network_errors
        has_mock_patterns = self._detect_mock_text(recon.body_snippet)

        return ModelPageInput(
            route=recon.route,
            has_data=False,  # Cannot determine from HTTP recon alone
            has_live_timestamps=False,
            has_js_errors=has_js_errors,
            has_network_errors=has_network_errors,
            has_mock_patterns=has_mock_patterns,
            has_feature_flag=False,
            visible_text=recon.body_snippet,
        )

    # ------------------------------------------------------------------
    # Phase 2 — Classification
    # ------------------------------------------------------------------

    def _classify_page(self, page: ModelPageInput) -> ModelPageStatus:
        """Classify a single page using the decision tree."""
        if page.has_js_errors or page.has_network_errors:
            return ModelPageStatus(
                route=page.route,
                status=EnumPageStatus.BROKEN,
                reason="JS error or network failure detected",
            )

        if page.has_mock_patterns or self._detect_mock_text(page.visible_text):
            return ModelPageStatus(
                route=page.route,
                status=EnumPageStatus.MOCK,
                reason="Mock/placeholder data detected",
            )

        if page.has_data and page.has_live_timestamps:
            return ModelPageStatus(
                route=page.route,
                status=EnumPageStatus.HEALTHY,
                reason="Real data with live timestamps",
            )

        if page.has_feature_flag:
            return ModelPageStatus(
                route=page.route,
                status=EnumPageStatus.FLAG_GATED,
                reason="Feature flag not set",
            )

        return ModelPageStatus(
            route=page.route,
            status=EnumPageStatus.EMPTY,
            reason="No data visible",
        )

    def _detect_mock_text(self, text: str) -> bool:
        """Check if visible text contains known mock patterns."""
        return any(p.search(text) for p in _MOCK_PATTERNS)

    # ------------------------------------------------------------------
    # Phase 3 — Triage
    # ------------------------------------------------------------------

    def _triage_domains(
        self,
        broken_pages: list[ModelPageStatus],
        all_pages: list[ModelPageInput],
    ) -> list[ModelProblemDomain]:
        """Group broken/empty pages into problem domains with fix tiers."""
        domains: list[ModelProblemDomain] = []
        page_input_map = {p.route: p for p in all_pages}

        for ps in broken_pages:
            page_input = page_input_map.get(ps.route)
            if not page_input:
                continue

            if ps.status == EnumPageStatus.BROKEN:
                if page_input.has_js_errors:
                    fix_tier = EnumFixTier.CODE_BUG
                    hypothesis = "JS error in page rendering"
                else:
                    fix_tier = EnumFixTier.CODE_BUG
                    hypothesis = "Network or API failure"
            else:
                fix_tier = EnumFixTier.DATA_PIPELINE
                hypothesis = "Data not reaching the page — pipeline gap"

            domain_id = ps.route.strip("/").replace("/", "-") or "root"
            domains.append(
                ModelProblemDomain(
                    domain_id=domain_id,
                    pages=[ps.route],
                    fix_tier=fix_tier,
                    hypothesis=hypothesis,
                )
            )

        return domains
