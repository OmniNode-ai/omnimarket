from pydantic import BaseModel, ConfigDict, Field


class ModelDodSweepOrchestratorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(
        default="",
        description=(
            "Ticket ID (OMN-XXXX) for targeted mode, or a project label / empty "
            "string to trigger batch mode. Batch mode requires ticket_ids or "
            "gh_search_query to enumerate tickets."
        ),
    )
    ticket_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Explicit list of ticket IDs for batch mode. When non-empty, overrides "
            "gh_search_query enumeration."
        ),
    )
    gh_search_query: str = Field(
        default="",
        description=(
            "GitHub search query passed to `gh issue list --search` for batch "
            "ticket enumeration. Used only when ticket_ids is empty and scope is "
            "not a direct OMN-XXXX ticket ID."
        ),
    )
    gh_repo: str = Field(
        default="",
        description=(
            "GitHub repository (owner/repo) used for PR and CI checks. Defaults to "
            "the repo inferred by the gh CLI from the current working directory. "
            "Prefer gh_repos for multi-repo tickets."
        ),
    )
    gh_repos: tuple[str, ...] = Field(
        default=(),
        description=(
            "Ordered list of GitHub repositories (owner/repo) to search for merged "
            "PRs. The handler tries each repo in turn and returns the first match. "
            "When non-empty, takes precedence over gh_repo for the PR search. "
            "Use this for tickets whose code PRs span multiple repositories."
        ),
    )
    evidence_root: str = Field(
        default="",
        description=(
            "Optional root for .evidence output. Defaults to ONEX_CC_REPO_PATH "
            "or the current working directory."
        ),
    )
    contract_root: str = Field(
        default="",
        description=(
            "Optional root containing contracts/. Defaults to ONEX_CC_REPO_PATH "
            "or the current working directory."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="When true, compute the receipt path but do not write it.",
    )
    enabled_checks: tuple[str, ...] = Field(
        default=("contract_exists", "receipt_exists", "pr_merged", "ci_green"),
        description=(
            "Which checks to run. Subset of: contract_exists, receipt_exists, "
            "pr_merged, ci_green."
        ),
    )
    gate_escape_audit: bool = Field(
        default=False,
        description=(
            "When true, run the L3 close-path gate-escape audit (OMN-13854) "
            "instead of the contract-based checks above: query recently-Done "
            "tickets and flag the wf_1628d9a5 signature (startedAt=null + zero "
            "attachments/documents + no merged PR discoverable), excluding the "
            "design-doc carve-outs."
        ),
    )
    audit_team: str = Field(
        default="Omninode",
        description="Linear team name to audit when gate_escape_audit is true.",
    )
    audit_since: str = Field(
        default="",
        description=(
            "ISO date/datetime lower bound (inclusive) for completedAt when "
            "enumerating Done tickets. Empty means no lower bound."
        ),
    )
    audit_until: str = Field(
        default="",
        description=(
            "ISO date/datetime upper bound (exclusive) for completedAt when "
            "enumerating Done tickets. Empty means no upper bound."
        ),
    )
    post_comment: bool = Field(
        default=False,
        description=(
            "When true (and dry_run is false), post a Linear comment on each "
            "flagged ticket. Never reopens or otherwise mutates ticket state."
        ),
    )
    linear_api_key: str = Field(
        default="",
        description=(
            "Optional Linear API key override for the gate-escape audit. "
            "Defaults to the LINEAR_API_KEY environment variable."
        ),
    )
