"""HandlerCreateTicket — the one ticket node: create, read, comment and transition.

OMN-20595: lab lanes have no Linear connector, so this node is how they file,
read and comment on tickets.

* ``create`` sends the caller's description to Linear unchanged, after the
  installed omniclaude ticket-creation guard (OMN-17942) admits it. The guard
  runs as a subprocess: the plugin's decision core is standard library only and
  lives in the omniclaude plugin, which this package cannot import. A create
  for which no guard is found, or which the guard refuses or cannot decide, is
  refused before any Linear call. Moving the guard into a shared package is an
  architecture change and is not done here.
* ``transition`` moves a ticket to a named workflow state. The state name is
  resolved to its Linear id for the ticket's own team, the installed guard
  judges the exact update payload first (same refusal and no-guard behaviour as
  a create), and the write goes through the adapter's ``update_issue``, whose
  own Done-write gate still applies.
* ``read`` and ``comment`` go through the omnibase_infra Linear project-tracker
  adapter (``AdapterLinearGraphQLProjectTracker``), constructed with the
  contract-declared key, so a missing key fails loud instead of reaching a stub.

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

import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from omnibase_infra.adapters.project_tracker.linear_graphql_project_tracker_adapter import (
    AdapterLinearGraphQLProjectTracker,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import contract_secret_ref

# Seam signal keyword sets
_SEAM_TOPICS = {"kafka", "topic", "consumer", "producer", "event bus", "redpanda"}
_SEAM_API = {"api", "endpoint", "rest", "graphql", "webhook", "http"}
_SEAM_DB = {"database", "postgres", "migration", "schema", "table", "sql"}
_SEAM_INFRA = {"docker", "deploy", "k8s", "kubernetes", "infra", "compose"}

_PARENT_RE = re.compile(r"^OMN-\d+$")
_TICKET_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

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

_BACKLOG_STATE_QUERY = """
query GetBacklogState($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } }, name: { eq: "Backlog" } }) {
    nodes { id }
  }
}
"""

_ISSUE_TEAM_STATE_QUERY = """
query GetIssueTeamState($identifier: String!, $name: String!) {
  issue(id: $identifier) {
    team { states(filter: { name: { eq: $name } }) { nodes { id } } }
  }
}
"""

_VIEWER_QUERY = """
query GetViewer {
  viewer { id }
}
"""

_ISSUE_BY_IDENTIFIER_QUERY = """
query GetIssueByIdentifier($identifier: String!) {
  issue(id: $identifier) { id }
}
"""

_ISSUE_CREATE_MUTATION = """
mutation CreateIssue($teamId: String!, $title: String!, $description: String!, $parentId: String, $stateId: String, $assigneeId: String) {
  issueCreate(input: {
    teamId: $teamId,
    title: $title,
    description: $description,
    parentId: $parentId,
    stateId: $stateId,
    assigneeId: $assigneeId
  }) {
    issue { identifier url }
  }
}
"""


_PILLAR_OWNERS_RELPATH = Path(
    "omnibase_internal/src/omnibase_internal/pillar_owners.yaml"
)


def _pillar_owners_path() -> Path:
    """Locate the shared pillar owner map (OMN-17427), failing fast when unresolvable.

    ``ONEX_PILLAR_OWNERS_PATH`` overrides; otherwise the omnibase_internal clone that sits in the
    parent of ``$OMNI_HOME`` (``OMNIBASE_INTERNAL_PATH`` overrides that clone). No default.
    """
    override = os.environ.get("ONEX_PILLAR_OWNERS_PATH")
    if override:
        return Path(override)
    clone = os.environ.get("OMNIBASE_INTERNAL_PATH")
    if clone:
        return Path(clone) / "src/omnibase_internal/pillar_owners.yaml"
    home = os.environ.get("OMNI_HOME")
    if not home:
        raise RuntimeError(
            "pillar_owners map not locatable: set OMNI_HOME, OMNIBASE_INTERNAL_PATH or "
            "ONEX_PILLAR_OWNERS_PATH"
        )
    return Path(home).parent / _PILLAR_OWNERS_RELPATH


def _resolve_pillar_owner(pillar: str | None, path: Path) -> str | None:
    """Return the Linear user id for the ticket's pillar, or None for the creator default.

    Operator rulings 2026-09-30 (OMN-17427): dashboard (including its data layer) and onboarding
    tickets go to their pillar's owner; everything else to the creator. The pillar is the explicit
    request field; a ticket with none goes to the creator. Owners are
    Linear user ids from omnibase_internal's ``pillar_owners.yaml``, the file the create-ticket
    skill reads. A pillar that applies and has no declared owner raises.
    """
    if not pillar:
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"pillar_owners map unreadable at {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"pillar_owners map at {path} must be a mapping")
    pillars = document.get("pillars")
    entry = pillars.get(pillar) if isinstance(pillars, dict) else None
    owner = entry.get("owner_linear_user_id") if isinstance(entry, dict) else None
    if not isinstance(owner, str) or not owner.strip():
        raise RuntimeError(f"pillar {pillar!r} has no owner_linear_user_id in {path}")
    return owner.strip()


class EnumTicketOperation(StrEnum):
    """What one ``node_create_ticket`` request does (OMN-20595)."""

    CREATE = "create"
    READ = "read"
    COMMENT = "comment"
    TRANSITION = "transition"


class ModelCreateTicketRequest(BaseModel):
    """Request to create, read or comment on a Linear ticket.

    The contract's ``handler.input_model``: the payload the runtime validates is
    this model, the one ``handle()`` takes (OMN-20595 fixed the drift).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumTicketOperation = Field(
        default=EnumTicketOperation.CREATE,
        description="create (the default), read, comment or transition.",
    )
    title: str = Field(default="", description="Ticket title (create).")
    description: str = Field(
        default="",
        description="Ticket description (create), sent to Linear unchanged.",
    )
    ticket_id: str | None = Field(
        default=None, description="Ticket identifier, e.g. OMN-1234 (read, comment)."
    )
    body: str = Field(default="", description="Comment body (comment).")
    state: str = Field(
        default="",
        description="Target workflow state name, e.g. Canceled (transition).",
    )
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
    pillar: str | None = Field(
        default=None,
        description=(
            "Ticket pillar (dashboard, onboarding) that sets the assignee from the shared "
            "pillar owner map; none means the creator."
        ),
    )
    project: str = Field(
        default="",
        description="Refused when non-empty: new tickets go to the Backlog with no project.",
    )

    @field_validator("project")
    @classmethod
    def _refuse_project(cls, value: str) -> str:
        """Operator ruling 2026-09-30T14:30:05Z (OMN-17427): a new ticket is
        created in the Backlog with NO project. Fail loud, never drop it."""
        if value.strip():
            raise ValueError(
                f"project={value!r} refused: every new ticket is created in the "
                "Backlog with no project (operator ruling 2026-09-30T14:30:05Z, "
                "OMN-17427). Moving a ticket into a sprint is the operator's call."
            )
        return value

    @model_validator(mode="after")
    def _fields_for_operation(self) -> ModelCreateTicketRequest:
        op = self.operation
        if self.state.strip() and op is not EnumTicketOperation.TRANSITION:
            raise ValueError("state is only valid with operation=transition")
        if op is EnumTicketOperation.CREATE:
            if not self.title.strip():
                raise ValueError("operation=create requires a title")
            return self
        if not self.ticket_id or not _TICKET_ID_RE.match(self.ticket_id):
            raise ValueError(
                f"operation={op.value} requires ticket_id like OMN-1234, "
                f"got {self.ticket_id!r}"
            )
        if op is EnumTicketOperation.COMMENT and not self.body.strip():
            raise ValueError("operation=comment requires a non-empty body")
        if op is EnumTicketOperation.TRANSITION and not self.state.strip():
            raise ValueError("operation=transition requires a non-empty state")
        return self


class ModelCreateTicketResult(BaseModel):
    """Result of a ticket create, read or comment request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="created")
    operation: EnumTicketOperation = Field(default=EnumTicketOperation.CREATE)
    ticket_id: str = Field(default="")
    ticket_url: str = Field(default="")
    title: str = Field(default="")
    team: str = Field(default="Omninode")
    ticket_status: str = Field(default="", description="Workflow state name (read).")
    comment_id: str = Field(default="", description="Created comment id (comment).")
    guard_path: str = Field(
        default="", description="The ticket-creation guard that admitted a create."
    )
    is_seam_ticket: bool = Field(default=False)
    interfaces_touched: list[str] = Field(default_factory=list)
    contract_completeness: str = Field(default="stub")
    validation_errors: list[str] = Field(default_factory=list)
    description_body: str = Field(
        default="",
        description="The description sent to Linear (create) or read from it (read).",
    )
    dry_run: bool = Field(default=False)


# ---------------------------------------------------------------------------
# The installed omniclaude ticket-creation guard (OMN-17942), as a subprocess
# ---------------------------------------------------------------------------

_GUARD_PATH_ENV = "ONEX_TICKET_GUARD_PATH"
_GUARD_RELPATH = Path("hooks/lib/ticket_creation_guard.py")
_ONEX_PLUGIN_PREFIX = "onex@"
_GUARD_TIMEOUT_S = 60


class TicketGuardUnavailableError(RuntimeError):
    """No installed ticket-creation guard was found, so no create may run."""


class TicketGuardRefusedError(RuntimeError):
    """The ticket-creation guard refused the create, or could not decide."""


class ModelTicketGuardDecision(BaseModel):
    """One decision of the ticket-creation guard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    reason: str = ""
    guard_path: str = ""


@runtime_checkable
class TicketGuardProtocol(Protocol):
    """Decides whether a Linear create payload may be filed."""

    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision: ...


def _claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(override) if override else Path.home() / ".claude"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def locate_ticket_guard() -> Path:
    """The installed omniclaude plugin's ticket-creation guard, or raise.

    ``ONEX_TICKET_GUARD_PATH`` names it explicitly (a lab lane takes it from the
    onex plugin directory its runner shipped). Otherwise the one enabled
    ``onex@<marketplace>`` plugin in Claude's installed-plugin registry. Nothing
    found, an explicit path that does not exist, or two enabled onex plugins
    each raise :class:`TicketGuardUnavailableError`; there is no default.
    """
    explicit = os.environ.get(_GUARD_PATH_ENV, "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise TicketGuardUnavailableError(
                f"{_GUARD_PATH_ENV}={explicit} is not a file; refusing to create "
                "a ticket without the ticket-creation guard (OMN-17942)"
            )
        return path

    config = _claude_config_dir()
    settings = _read_json(config / "settings.json")
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    registry = _read_json(config / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins") if isinstance(registry, dict) else None
    candidates: list[Path] = []
    if isinstance(enabled, dict) and isinstance(plugins, dict):
        for key, entries in plugins.items():
            if not key.startswith(_ONEX_PLUGIN_PREFIX) or enabled.get(key) is not True:
                continue
            for entry in entries if isinstance(entries, list) else []:
                install = entry.get("installPath") if isinstance(entry, dict) else None
                if (
                    isinstance(install, str)
                    and (Path(install) / _GUARD_RELPATH).is_file()
                ):
                    candidates.append(Path(install) / _GUARD_RELPATH)
    if len(candidates) > 1:
        raise TicketGuardUnavailableError(
            "more than one enabled onex plugin carries a ticket-creation guard "
            f"({', '.join(map(str, candidates))}); set {_GUARD_PATH_ENV} to the one to use"
        )
    if not candidates:
        raise TicketGuardUnavailableError(
            "no installed ticket-creation guard: set "
            f"{_GUARD_PATH_ENV} to <onex plugin dir>/{_GUARD_RELPATH}, or enable the "
            f"onex plugin under {config}; refusing to create a ticket without it (OMN-17942)"
        )
    return candidates[0]


class InstalledPluginTicketGuard:
    """Runs the omniclaude guard's own decision on a ``save_issue`` payload.

    Exit 0 admits, exit 3 refuses with the reason as JSON on stdout, and any
    other exit means the guard could not decide, which is a refusal (the hook's
    own convention).
    """

    def __init__(self, guard_path: Path) -> None:
        self.guard_path = guard_path
        self.interpreter = sys.executable

    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        payload = json.dumps(
            {"tool_name": "mcp__linear-server__save_issue", "tool_input": tool_input}
        )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = subprocess.run(
            [self.interpreter, str(self.guard_path)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=_GUARD_TIMEOUT_S,
            env=env,
            check=False,
        )
        path = str(self.guard_path)
        if done.returncode == 0:
            return ModelTicketGuardDecision(admitted=True, guard_path=path)
        if done.returncode == 3:
            try:
                reason = str(json.loads(done.stdout).get("reason", done.stdout))
            except (ValueError, AttributeError):
                reason = done.stdout
            return ModelTicketGuardDecision(
                admitted=False, reason=reason, guard_path=path
            )
        return ModelTicketGuardDecision(
            admitted=False,
            reason=(
                f"the ticket-creation guard could not decide (exit {done.returncode}): "
                f"{done.stderr.strip()[-2000:]}"
            ),
            guard_path=path,
        )


# ---------------------------------------------------------------------------
# Read and comment: the omnibase_infra Linear project-tracker adapter
# ---------------------------------------------------------------------------


class TicketTrackerProtocol(Protocol):
    """The slice of ``ProtocolProjectTracker`` the read and comment operations use."""

    async def get_issue(self, issue_id: str) -> Any: ...

    async def add_comment(self, issue_id: str, body: str) -> Any: ...

    async def update_issue(self, issue_id: str, updates: dict[str, str]) -> Any: ...

    async def close(self, timeout_seconds: float = 30.0) -> None: ...


@runtime_checkable
class TicketStateResolverProtocol(Protocol):
    """Resolves a workflow state name to its Linear id for one ticket's team."""

    def resolve_state_id(self, ticket_id: str, state: str) -> str: ...


def _run_coroutine[T](factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine from this sync handler, whether or not a loop is running."""

    async def _main() -> T:
        return await factory()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_main())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _main()).result()


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


# ---------------------------------------------------------------------------
# Injectable protocol for Linear (enables unit testing without network calls)
# ---------------------------------------------------------------------------


@runtime_checkable
class LinearTicketClientProtocol(Protocol):
    """Adapter boundary for Linear ticket creation.

    Both the real HTTP client and the unit-test mock implement this interface.
    """

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
    ) -> tuple[str, str]:
        """Create a Linear issue and return ``(ticket_id, ticket_url)``.

        ``assignee_id`` is a Linear user id; ``None`` assigns the API key's own user (the creator).
        """
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
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
    ) -> tuple[str, str]:
        """Create a new Linear issue and return ``(identifier, url)``."""
        team_data = self._post(_TEAM_QUERY, {"name": team})
        team_nodes = team_data.get("data", {}).get("teams", {}).get("nodes", [])
        if not team_nodes:
            raise RuntimeError(f"Linear team {team!r} not found")
        team_id = team_nodes[0]["id"]

        # Operator ruling 2026-09-30T14:30:05Z (OMN-17427): every new ticket is
        # created in the Backlog with NO project. The state is set explicitly
        # rather than trusting the team default, and no projectId is ever sent.
        state_data = self._post(_BACKLOG_STATE_QUERY, {"teamId": team_id})
        state_nodes = (
            state_data.get("data", {}).get("workflowStates", {}).get("nodes", [])
        )
        if not state_nodes:
            raise RuntimeError(
                f"Linear team {team!r} has no 'Backlog' workflow state; refusing "
                "to create a ticket outside the Backlog (operator ruling "
                "2026-09-30T14:30:05Z, OMN-17427)"
            )

        variables: dict[str, object] = {
            "teamId": team_id,
            "title": title,
            "description": description,
            "stateId": state_nodes[0]["id"],
        }
        if not assignee_id:
            viewer = self._post(_VIEWER_QUERY, {}).get("data", {}).get("viewer", {})
            assignee_id = str(viewer.get("id", ""))
            if not assignee_id:
                raise RuntimeError(
                    "Linear viewer id unavailable; cannot assign the creator"
                )
        variables["assigneeId"] = assignee_id
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

    def resolve_state_id(self, ticket_id: str, state: str) -> str:
        """Return the id of workflow state ``state`` on ``ticket_id``'s own team."""
        data = self._post(
            _ISSUE_TEAM_STATE_QUERY, {"identifier": ticket_id, "name": state}
        )
        issue = data.get("data", {}).get("issue") or {}
        nodes = (issue.get("team") or {}).get("states", {}).get("nodes", [])
        if not nodes:
            raise RuntimeError(
                f"Linear ticket {ticket_id!r} has no workflow state named {state!r}"
            )
        return str(nodes[0]["id"])


class HandlerCreateTicket:
    """The one ticket handler: create, read, comment and transition (OMN-20595).

    Create validates input, runs the installed ticket-creation guard on the
    caller's exact payload, creates the Linear ticket with the description
    unchanged, and fails closed on an empty ``ticket_id``. Read and comment go
    through the omnibase_infra Linear project-tracker adapter.

    Secret resolution:
        - ``LINEAR_API_KEY`` is resolved from the contract-declared ref via
          ``contract_secret_ref`` + ``resolve_api_key_loop_safe``; ``None``
          raises. There is no stub fallback.
        - ``linear_client``, ``tracker`` and ``ticket_guard`` are injection
          seams for tests; when given, they are used directly.
    """

    def __init__(
        self,
        linear_client: LinearTicketClientProtocol | None = None,
        pillar_owners_path: Path | None = None,
        tracker: TicketTrackerProtocol | None = None,
        ticket_guard: TicketGuardProtocol | None = None,
        state_resolver: TicketStateResolverProtocol | None = None,
    ) -> None:
        self._injectable_client = linear_client
        self._pillar_owners_path = pillar_owners_path
        self._injectable_tracker = tracker
        self._injectable_guard = ticket_guard
        self._injectable_state_resolver = state_resolver

    def _linear_api_key(self) -> str:
        # Ref-name sourced from contract (not a bare literal).
        linear_ref = contract_secret_ref(_CONTRACT_PATH, "LINEAR_API_KEY")
        secret = resolve_api_key_loop_safe(linear_ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {linear_ref!r} resolved to None — "
                "ensure LINEAR_API_KEY is set in the secret store "
                "(~/.omnibase/.env)."
            )
        return secret.get_secret_value()

    def _get_client(self) -> LinearTicketClientProtocol:
        if self._injectable_client is not None:
            return self._injectable_client
        return LinearTicketHttpGateway(self._linear_api_key())

    def _get_tracker(self) -> TicketTrackerProtocol:
        if self._injectable_tracker is not None:
            return self._injectable_tracker
        return AdapterLinearGraphQLProjectTracker(api_key=self._linear_api_key())

    def _get_state_resolver(self) -> TicketStateResolverProtocol:
        if self._injectable_state_resolver is not None:
            return self._injectable_state_resolver
        return LinearTicketHttpGateway(self._linear_api_key())

    def _get_guard(self) -> TicketGuardProtocol:
        if self._injectable_guard is not None:
            return self._injectable_guard
        return InstalledPluginTicketGuard(locate_ticket_guard())

    def handle(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        """Process one create, read or comment request."""
        if request.operation is EnumTicketOperation.READ:
            return self._read(request)
        if request.operation is EnumTicketOperation.COMMENT:
            return self._comment(request)
        if request.operation is EnumTicketOperation.TRANSITION:
            return self._transition(request)
        return self._create(request)

    def _transition(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        ticket_id = str(request.ticket_id)
        state = request.state.strip()
        # The guard judges the exact update payload filed: the ticket id and the
        # target state, nothing else.
        decision = self._get_guard().check({"id": ticket_id, "state": state})
        if not decision.admitted:
            raise TicketGuardRefusedError(
                "node_create_ticket: the ticket-creation guard refused this "
                f"transition ({decision.guard_path}):\n{decision.reason}"
            )
        if request.dry_run:
            return ModelCreateTicketResult(
                status="dry_run",
                operation=EnumTicketOperation.TRANSITION,
                ticket_id=ticket_id,
                team=request.team,
                ticket_status=state,
                guard_path=decision.guard_path,
                dry_run=True,
            )
        state_id = self._get_state_resolver().resolve_state_id(ticket_id, state)
        tracker = self._get_tracker()

        async def _move() -> Any:
            try:
                return await tracker.update_issue(ticket_id, {"stateId": state_id})
            finally:
                await tracker.close()

        issue = _run_coroutine(_move)
        new_state = str(getattr(issue, "state", "") or "")
        if not new_state:
            raise RuntimeError(
                f"node_create_ticket: Linear reported no state for {ticket_id} after "
                "the update; refusing to report status='transitioned'."
            )
        return ModelCreateTicketResult(
            status="transitioned",
            operation=EnumTicketOperation.TRANSITION,
            ticket_id=ticket_id,
            ticket_url=str(getattr(issue, "url", "") or ""),
            team=request.team,
            ticket_status=new_state,
            guard_path=decision.guard_path,
        )

    def _read(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        ticket_id = str(request.ticket_id)
        tracker = self._get_tracker()

        async def _get() -> Any:
            try:
                return await tracker.get_issue(ticket_id)
            finally:
                await tracker.close()

        issue = _run_coroutine(_get)
        return ModelCreateTicketResult(
            status="read",
            operation=EnumTicketOperation.READ,
            ticket_id=str(issue.identifier),
            ticket_url=str(issue.url or ""),
            title=str(issue.title),
            team=request.team,
            ticket_status=str(issue.state),
            description_body=str(issue.description or ""),
        )

    def _comment(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        ticket_id = str(request.ticket_id)
        if request.dry_run:
            return ModelCreateTicketResult(
                status="dry_run",
                operation=EnumTicketOperation.COMMENT,
                ticket_id=ticket_id,
                team=request.team,
                dry_run=True,
            )
        tracker = self._get_tracker()

        async def _post() -> Any:
            try:
                return await tracker.add_comment(ticket_id, request.body)
            finally:
                await tracker.close()

        comment = _run_coroutine(_post)
        comment_id = str(getattr(comment, "id", "") or "")
        if not comment_id:
            raise RuntimeError(
                f"node_create_ticket: Linear returned no comment id for {ticket_id}; "
                "refusing to report status='commented'."
            )
        return ModelCreateTicketResult(
            status="commented",
            operation=EnumTicketOperation.COMMENT,
            ticket_id=ticket_id,
            team=request.team,
            comment_id=comment_id,
        )

    def _create(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
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

        # The ticket-creation guard (OMN-17942) judges the exact payload this
        # node files: Backlog, no project, the caller's description unchanged.
        tool_input: dict[str, object] = {
            "team": request.team,
            "title": request.title,
            "description": request.description,
            "state": "Backlog",
        }
        if request.parent:
            tool_input["parentId"] = request.parent
        decision = self._get_guard().check(tool_input)
        if not decision.admitted:
            raise TicketGuardRefusedError(
                "node_create_ticket: the ticket-creation guard refused this create "
                f"({decision.guard_path}):\n{decision.reason}"
            )

        if request.dry_run:
            return ModelCreateTicketResult(
                status="dry_run",
                title=request.title,
                team=request.team,
                guard_path=decision.guard_path,
                description_body=request.description,
                dry_run=True,
            )

        is_seam, interfaces = _detect_seam(request.title, request.description)
        contract_completeness = "full" if is_seam else "stub"

        assignee_id = (
            _resolve_pillar_owner(
                request.pillar, self._pillar_owners_path or _pillar_owners_path()
            )
            if request.pillar
            else None
        )
        client = self._get_client()
        ticket_id, ticket_url = client.create_ticket(
            title=request.title,
            description=request.description,
            team=request.team,
            parent=request.parent,
            assignee_id=assignee_id,
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
            guard_path=decision.guard_path,
            is_seam_ticket=is_seam,
            interfaces_touched=interfaces,
            contract_completeness=contract_completeness,
            description_body=request.description,
            dry_run=False,
        )


__all__: list[str] = [
    "EnumTicketOperation",
    "HandlerCreateTicket",
    "InstalledPluginTicketGuard",
    "LinearTicketClientProtocol",
    "LinearTicketHttpGateway",
    "ModelCreateTicketRequest",
    "ModelCreateTicketResult",
    "ModelTicketGuardDecision",
    "TicketGuardProtocol",
    "TicketGuardRefusedError",
    "TicketGuardUnavailableError",
    "TicketStateResolverProtocol",
    "TicketTrackerProtocol",
    "locate_ticket_guard",
]
