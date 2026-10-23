"""Retired change-control writers cannot be discovered or dispatched."""

import tomllib
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
RETIRED_NODES = (
    "node_occ_companion_compute",
    "node_occ_companion_effect",
    "node_occ_state_effect",
    "node_occ_autoauthor_window",
    "node_occ_attestation_observe",
    "node_occ_observation_effect",
    "node_occ_observation_projection",
    "node_occ_observation_source_effect",
    "node_occ_pr_writer_effect",
    "node_occ_evidence_draft_orchestrator",
    "node_occ_evidence_validator_compute",
)


@pytest.mark.parametrize("name", RETIRED_NODES)
def test_retired_node_is_neither_packaged_nor_registered(name: str) -> None:
    entries = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert not (ROOT / "src/omnimarket/nodes" / name).exists()
    assert name not in entries
    assert "node_dod_verify" in entries


def test_repo_contract_binding_fields_need_no_other_repository(tmp_path: Path) -> None:
    from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
        EvidenceCollector,
    )

    item = {
        "id": "local",
        "description": "local evidence",
        "checks": [],
        "ac_bindings": [],
        "supersedes_ac_binding": [],
    }
    assert EvidenceCollector._validate_evidence_audiences([item], tmp_path) == []
    refused = EvidenceCollector._validate_evidence_audiences(
        [{**item, "execution_scpoe": "hosted_and_local"}], tmp_path
    )
    assert len(refused) == 1
    assert "INVALID_DOD_EVIDENCE_ITEM" in refused[0].message


@pytest.mark.asyncio
async def test_legacy_companion_command_refuses_without_a_writer() -> None:
    from datetime import UTC, datetime

    from omnimarket.events.pr_lifecycle_fix.model_fix_command import EnumPrBlockReason
    from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
        HandlerPrLifecycleFixRuntime,
    )
    from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
        ModelPrLifecycleFixCommand,
    )

    command = ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        repo="OmniNode-ai/omnimarket",
        pr_number=1,
        block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
        dry_run=False,
        requested_at=datetime.now(UTC),
    )
    calls = []

    class Writer:
        async def autobind_evidence_source(
            self, *args: object, **kwargs: object
        ) -> str:
            calls.append((args, kwargs))
            return "unexpected writer invocation"

    runtime = HandlerPrLifecycleFixRuntime(
        occ_autobind_adapter=Writer(),
        outcome_token_resolver=lambda: None,
        head_sha_resolver=lambda *_args: None,
    )
    output = await runtime.handle(command)
    result = output.events[0]
    assert calls == []
    assert result.fix_applied is False
    assert result.error is not None
    assert "retired" in result.error.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_matches", "checks_pass", "expected"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
async def test_product_evidence_readback_is_bound_to_current_head(
    source_matches: bool, checks_pass: bool, expected: bool
) -> None:
    # The before/after control runs this same test on the merge base, where
    # the former reader name exists. Fail on its behavior rather than import.
    try:
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.pr_evidence_readback import (
            PrEvidenceReadback,
        )
    except ModuleNotFoundError as exc:
        if (
            exc.name
            != "omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.pr_evidence_readback"
        ):
            raise
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.occ_stamp_readback import (
            OccStampReadback as PrEvidenceReadback,
        )

    head = "a" * 40
    calls = []

    class Fetcher:
        def fetch_pr(self, repo: str, pr_number: int) -> dict[str, object]:
            calls.append((repo, pr_number))
            source = head if source_matches else "invalid"
            return {
                "body": "Closes OMN-17427",
                "state": "OPEN",
                "headRefOid": source,
                "statusCheckRollup": [
                    {
                        "name": name,
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS" if checks_pass else "FAILURE",
                    }
                    for name in ("repo-evidence / verify", "repo-evidence / dod-verify")
                ],
            }

    result = await PrEvidenceReadback(fetcher=Fetcher()).verify_fix_landed(
        "OmniNode-ai/omnimarket", 1, "OMN-17427"
    )
    assert result.verified is expected
    assert calls == [("OmniNode-ai/omnimarket", 1)]


@pytest.mark.asyncio
async def test_normal_runtime_fixes_still_deduplicate_completed_commands() -> None:
    from datetime import UTC, datetime

    from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
        HandlerPrLifecycleFixRuntime,
    )
    from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
        EnumPrBlockReason,
        ModelPrLifecycleFixCommand,
    )

    class GitHub:
        calls = 0

        async def rerun_failed_checks(self, repo: str, pr_number: int) -> str:
            self.calls += 1
            return f"rerun {repo}#{pr_number}"

        async def update_branch(self, repo: str, pr_number: int) -> str:
            raise AssertionError("unrequested branch update")

    adapter = GitHub()
    runtime = HandlerPrLifecycleFixRuntime(github_adapter=adapter)
    command = ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        repo="OmniNode-ai/omnimarket",
        pr_number=1,
        block_reason=EnumPrBlockReason.CI_FAILURE,
        requested_at=datetime.now(UTC),
    )
    first = await runtime.handle(command)
    second = await runtime.handle(command)
    assert first.events == second.events
    assert adapter.calls == 1
