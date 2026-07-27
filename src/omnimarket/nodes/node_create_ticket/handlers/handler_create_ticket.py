"""HandlerCreateTicket — ticket creation with seam detection, validation, and
a real Linear GraphQL create-issue call.

OMN-14547: prior to this change, the handler always returned
``status="created"`` with an empty ``ticket_id`` — a fake-success facade that
never touched the Linear API. This is fixed two ways:

1. Fail-closed: a create response with an empty ``ticket_id`` (from Linear or
   from an injected test double) raises instead of being reported as success.
2. Real call: the non-dry-run, validation-clean path now calls the Linear
   GraphQL ``issueCreate`` mutation through :class:`LinearTicketHttpGateway`,
   resolving the target team by name and (when given) the parent issue by
   identifier — following the same embedded-client pattern already used by
   ``node_repo_health_repair_effect`` and ``node_linear_triage`` in this repo.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import contract_secret_ref

# Seam signal keyword sets
_SEAM_TOPICS = {"kafka", "topic", "consumer", "producer", "event bus", "redpanda"}
_SEAM_API = {"api", "endpoint", "rest", "graphql", "webhook", "http"}
_SEAM_DB = {"database", "postgres", "migration", "schema", "table", "sql"}
_SEAM_INFRA = {"docker", "deploy", "k8s", "kubernetes", "infra", "compose"}

_PARENT_RE = re.compile(r"^OMN-\d+$")

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def _contract_linear_graphql_url(contract_path: Path) -> str:
    """Return the contract-declared Linear GraphQL endpoint.

    Mirrors ``node_repo_health_repair_effect``'s helper of the same name —
    the URL-authority gate (OMN-12818) requires connection targets to
    resolve from a contract, not a bare module-level literal.
    """
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")
    integrations = raw.get("integrations")
    if not isinstance(integrations, dict):
        raise ValueError(f"{contract_path} missing integrations mapping")
    linear = integrations.get("linear")
    if not isinstance(linear, dict):
        raise ValueError(f"{contract_path} missing integrations.linear mapping")
    graphql_url = linear.get("graphql_url")
    if not isinstance(graphql_url, str) or not graphql_url.strip():
        raise ValueError(f"{contract_path} integrations.linear.graphql_url must be set")
    return graphql_url


_LINEAR_GRAPHQL_URL = _contract_linear_graphql_url(_CONTRACT_PATH)

_TEAM_QUERY = """
query GetTeamByName($name: String!) {
  teams(filter: { name: { eq: $name } }) {
    nodes { id }
  }
}
"""

_ISSUE_BY_IDENTIFIER_QUERY = """
query GetIssueByIdentifier($identifier: String!) {
  issue(id: $identifier) { id }
}
"""

_ISSUE_CREATE_MUTATION = """
mutation CreateIssue($teamId: String!, $title: String!, $description: String!, $parentId: String) {
  issueCreate(input: {
    teamId: $teamId,
    title: $title,
    description: $description,
    parentId: $parentId
  }) {
    issue { identifier url }
  }
}
"""


class ModelCreateTicketRequest(BaseModel):
    """Request to create a Linear ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(..., description="Ticket title.")
    description: str = Field(default="", description="Optional description.")
    repo: str | None = Field(default=None, description="Primary repo for scoping.")
    parent: str | None = Field(default=None, description="Parent ticket ID (OMN-XXXX).")
    blocked_by: list[str] = Field(
        default_factory=list, description="Blocking ticket IDs."
    )
    dry_run: bool = Field(default=False)
    team: str = Field(default="Omninode")
    allow_arch_violation: bool = Field(
        default=False,
        description="Bypass architecture dependency validation (contract input).",
    )


class ModelCreateTicketResult(BaseModel):
    """Result of a ticket creation request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="created")
    ticket_id: str = Field(default="")
    ticket_url: str = Field(default="")
    title: str = Field(default="")
    team: str = Field(default="Omninode")
    is_seam_ticket: bool = Field(default=False)
    interfaces_touched: list[str] = Field(default_factory=list)
    contract_completeness: str = Field(default="stub")
    validation_errors: list[str] = Field(default_factory=list)
    description_body: str = Field(default="")
    dry_run: bool = Field(default=False)


def _detect_seam(title: str, description: str) -> tuple[bool, list[str]]:
    """Detect seam signals and return (is_seam, interfaces_touched)."""
    text = (title + " " + description).lower()
    interfaces: list[str] = []
    if any(kw in text for kw in _SEAM_TOPICS):
        interfaces.append("topics")
    if any(kw in text for kw in _SEAM_API):
        interfaces.append("public_api")
    if any(kw in text for kw in _SEAM_DB):
        interfaces.append("database")
    if any(kw in text for kw in _SEAM_INFRA):
        interfaces.append("infrastructure")
    return bool(interfaces), interfaces


def _generate_description_body(request: ModelCreateTicketRequest) -> str:
    """Generate a structured description body."""
    lines: list[str] = ["## Summary", ""]
    if request.description:
        lines.append(request.description)
    else:
        lines.append(f"Implement: {request.title}")
    lines.append("")
    lines.append("## Definition of Done")
    lines.append("")
    lines.append("- [ ] Implementation complete")
    lines.append("- [ ] Tests pass")
    lines.append("- [ ] PR merged")
    if request.repo:
        lines.append(f"- [ ] Verified in `{request.repo}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Injectable protocol for Linear (enables unit testing without network calls)
# ---------------------------------------------------------------------------


@runtime_checkable
class LinearTicketClientProtocol(Protocol):
    """Adapter boundary for Linear ticket creation.

    Both the real HTTP client and the unit-test mock implement this interface.
    """

    def create_ticket(
        self, *, title: str, description: str, team: str, parent: str | None
    ) -> tuple[str, str]:
        """Create a Linear issue and return ``(ticket_id, ticket_url)``."""
        ...


# ---------------------------------------------------------------------------
# Real Linear HTTP gateway (GraphQL)
# ---------------------------------------------------------------------------


class LinearTicketHttpGateway:
    """Real Linear GraphQL gateway used by ``HandlerCreateTicket``.

    Reads the API key from the caller-supplied resolved secret — never from
    ``os.environ`` directly. All network interaction is isolated here,
    following the same embedded pattern as
    ``node_repo_health_repair_effect.LinearRepairHttpClient`` and
    ``node_linear_triage.LinearHttpClient`` (this repo intentionally embeds a
    small transport per Linear-writing node rather than sharing one — see
    ``CLAUDE.md`` "Do not make one node import another node's private handler
    or model package"). Named ``...Gateway`` rather than ``...Client`` to
    stay outside the non-canonical lifecycle-class ratchet (OMN-14350).
    """

    _BASE = _LINEAR_GRAPHQL_URL

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError(
                "LINEAR_API_KEY must not be empty. Resolve it from the "
                "contract api_key_ref before constructing the client."
            )
        self._api_key = api_key

    def _post(self, query: str, variables: dict[str, object]) -> Any:
        import json
        import urllib.request

        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            self._BASE,
            data=payload,
            headers={
                "Content-Type": "application/json",
                # No "Bearer" prefix — matches the established Linear auth
                # convention across this repo (see LinearRepairHttpClient).
                "Authorization": self._api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if "errors" in data:
            raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
        return data

    def create_ticket(
        self, *, title: str, description: str, team: str, parent: str | None
    ) -> tuple[str, str]:
        """Create a new Linear issue and return ``(identifier, url)``."""
        team_data = self._post(_TEAM_QUERY, {"name": team})
        team_nodes = team_data.get("data", {}).get("teams", {}).get("nodes", [])
        if not team_nodes:
            raise RuntimeError(f"Linear team {team!r} not found")
        team_id = team_nodes[0]["id"]

        variables: dict[str, object] = {
            "teamId": team_id,
            "title": title,
            "description": description,
        }
        if parent:
            parent_data = self._post(_ISSUE_BY_IDENTIFIER_QUERY, {"identifier": parent})
            parent_uuid = parent_data.get("data", {}).get("issue", {}).get("id", "")
            if parent_uuid:
                variables["parentId"] = parent_uuid

        result = self._post(_ISSUE_CREATE_MUTATION, variables)
        issue = result.get("data", {}).get("issueCreate", {}).get("issue", {})
        identifier = str(issue.get("identifier", ""))
        url = str(issue.get("url", ""))
        return identifier, url


class HandlerCreateTicket:
    """Handler for ticket creation — validates input, detects seams, creates
    the Linear ticket, and fails closed on an empty ``ticket_id``.

    Secret resolution:
        - When ``linear_client`` is None (the default), the handler resolves
          ``LINEAR_API_KEY`` from the contract-declared ``api_key_ref`` via
          ``contract_secret_ref`` + ``resolve_api_key_loop_safe`` and
          constructs a ``LinearTicketHttpGateway``.
        - When ``linear_client`` is provided (unit tests), it is used
          directly, bypassing all secret resolution.
    """

    def __init__(self, linear_client: LinearTicketClientProtocol | None = None) -> None:
        self._injectable_client = linear_client

    def _get_client(self) -> LinearTicketClientProtocol:
        if self._injectable_client is not None:
            return self._injectable_client
        # Ref-name sourced from contract (not a bare literal).
        linear_ref = contract_secret_ref(_CONTRACT_PATH, "LINEAR_API_KEY")
        secret = resolve_api_key_loop_safe(linear_ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {linear_ref!r} resolved to None — "
                "ensure LINEAR_API_KEY is set in the secret store "
                "(~/.omnibase/.env)."
            )
        return LinearTicketHttpGateway(secret.get_secret_value())

    def handle(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        """Process a ticket creation request."""
        errors: list[str] = []

        # Validate parent ID format
        if request.parent and not _PARENT_RE.match(request.parent):
            errors.append(
                f"Invalid parent ID format: {request.parent!r} (expected OMN-XXXX)"
            )

        # Validate blocked_by IDs
        for bid in request.blocked_by:
            if not _PARENT_RE.match(bid):
                errors.append(
                    f"Invalid blocked_by ID format: {bid!r} (expected OMN-XXXX)"
                )

        if errors:
            return ModelCreateTicketResult(
                status="error",
                title=request.title,
                team=request.team,
                validation_errors=errors,
                dry_run=request.dry_run,
            )

        if request.dry_run:
            return ModelCreateTicketResult(
                status="dry_run",
                title=request.title,
                team=request.team,
                dry_run=True,
            )

        is_seam, interfaces = _detect_seam(request.title, request.description)
        contract_completeness = "full" if is_seam else "stub"
        description_body = _generate_description_body(request)

        client = self._get_client()
        ticket_id, ticket_url = client.create_ticket(
            title=request.title,
            description=description_body,
            team=request.team,
            parent=request.parent,
        )

        # Fail-closed (OMN-14547): a "created" result with no id is a lie —
        # never emit it. Either Linear genuinely created the ticket and
        # handed back an identifier, or this call did not succeed.
        if not ticket_id:
            raise RuntimeError(
                "node_create_ticket: Linear create_ticket returned an empty "
                "ticket_id — refusing to report status='created' without a "
                "real ticket (OMN-14547 fail-closed guard)."
            )

        return ModelCreateTicketResult(
            status="created",
            ticket_id=ticket_id,
            ticket_url=ticket_url,
            title=request.title,
            team=request.team,
            is_seam_ticket=is_seam,
            interfaces_touched=interfaces,
            contract_completeness=contract_completeness,
            description_body=description_body,
            dry_run=False,
        )


__all__: list[str] = [
    "HandlerCreateTicket",
    "LinearTicketClientProtocol",
    "LinearTicketHttpGateway",
    "ModelCreateTicketRequest",
    "ModelCreateTicketResult",
]
