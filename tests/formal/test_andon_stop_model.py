# SPDX-License-Identifier: MIT
"""OMN-21442: the andon stop model, its TLC results and its contracts agree.

Two machines are modelled in one module: the stop (open, swarming, fixed, stale,
cleared, declared_false) and the lane lease a swarm lane shares with a bump
owner (claimed, spawning, uncertain, active, expired, replaced, released).

The committed results must be for the committed model, configs and outputs; the
design configs must pass every property; each property must fail on a mutation
that flips exactly one design knob, shown by the mutant's own TLC output; each
witness config must show its state is reachable; the model's states, triggers,
transitions and constants must be those of each machine's own contract; and the
model and its configs must name no store, bus, host or path. The invariants and
the action property are checked by the design config; the liveness properties by
the design_live config, which differs only in exploration bounds. Rerun
``tests/formal/andon_stop_tlc.sh`` after changing the model or a config.

The contracts of the two machines come from their nodes' ``contract.yaml`` once
those nodes exist; until then the fixtures under ``tests/formal/fixtures`` carry
the same ``state_machine`` and ``config`` fragments.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "formal" / "andon_stop"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "AndonStop.tla"
DESIGN = "AndonStop_design"
DESIGN_LIVE = "AndonStop_design_live"

INVARIANTS = (
    "OneStopPerKey",
    "StormIsOneStop",
    "ReplacementsBounded",
    "NeverClearsWithoutReadings",
    "UnknownNeverClears",
    "OnlyPostCountermeasureCounts",
    "FixedHasAnchor",
    "NoHumanClear",
    "MigrationNeverEntersTerminal",
    "HandFixOnlyFirstWithEvidence",
    "StaleEmitsDecisionRow",
    "DeclaredFalseIsRecorded",
    "GateSeesOpenStop",
    "StopRefusesNewDispatchOnly",
    "SwarmLaneNeverRefused",
    "OnlyBoundExemptionsAdmitted",
    "NewDispatchRefusedWhileOpen",
    "ObserveNeverRefuses",
    "CanaryAdmittedWhileOpen",
    "AtMostOneActiveLease",
    "ActiveRequiresStartReceipt",
    "StaleGenerationWriteRefused",
    "WriteOnlyUnderActiveLease",
)
ACTION = ("StaleNeverClears",)
TEMPORAL = (
    "StopResolves",
    "PendingLeaseResolves",
    "ExpiredReplacementNeverWaits",
    "UnreadableFixedReachesStale",
)

# mutant config -> (the one design knob it flips, the property it must violate)
MUTANTS = {
    "M1_grant_unguarded": ("GrantGuard", "ReplacementsBounded"),
    "M2_expiry_unguarded": ("ExpiryGuard", "ReplacementsBounded"),
    "M3_reopen_resets_budget": ("ReopenResetsBudget", "ReplacementsBounded"),
    "M4_handfix_clears": ("HandFixTarget", "NeverClearsWithoutReadings"),
    "M5_clear_on_any_reading": ("ClearGuard", "NeverClearsWithoutReadings"),
    "M6_unknown_counts": ("UnknownAs", "UnknownNeverClears"),
    "M7_history_counts": ("CrossingsAfter", "OnlyPostCountermeasureCounts"),
    "M8_handfix_no_evidence": ("HandFixNeedsEvidence", "HandFixOnlyFirstWithEvidence"),
    "M9_handfix_any_occurrence": ("HandFixNeedsFirst", "HandFixOnlyFirstWithEvidence"),
    "M10_handfix_no_anchor": ("HandFixAnchors", "FixedHasAnchor"),
    "M11_stale_clears_itself": ("StaleClears", "StaleNeverClears"),
    "M12_stale_no_decision_row": ("StaleDecisionRow", "StaleEmitsDecisionRow"),
    "M13_operator_clears": ("OperatorClear", "NoHumanClear"),
    "M14_declare_unrecorded": ("DeclareRecorded", "DeclaredFalseIsRecorded"),
    "M15_migration_clears": ("MigrationMap", "MigrationNeverEntersTerminal"),
    "M16_restart_forgets": ("RestartMode", "GateSeesOpenStop"),
    "M17_dedup_by_detector": ("DedupBy", "OneStopPerKey"),
    "M18_storm_per_key": ("StormCollapse", "StormIsOneStop"),
    "M19_refuse_open_pr_fix": ("Exempt", "StopRefusesNewDispatchOnly"),
    "M20_refuse_swarm_lane": ("Exempt", "SwarmLaneNeverRefused"),
    "M21_claim_by_brief": ("ExemptionBy", "OnlyBoundExemptionsAdmitted"),
    "M22_gate_skips_stale": ("GatedStates", "NewDispatchRefusedWhileOpen"),
    "M23_observe_refuses": ("ObserveMode", "ObserveNeverRefuses"),
    "M24_no_canary": ("Exempt", "CanaryAdmittedWhileOpen"),
    "M25_claim_unguarded": ("ClaimGuard", "AtMostOneActiveLease"),
    "M26_receipt_unguarded": ("ReceiptGuard", "ActiveRequiresStartReceipt"),
    "M27_no_generation_check": ("GenCheck", "StaleGenerationWriteRefused"),
    "M28_revoke_on_replacement": ("RevokeOn", "WriteOnlyUnderActiveLease"),
    "M29_no_stale_limit": ("StaleLimit", "StopResolves"),
    "M30_no_pending_window": ("PendingWindow", "PendingLeaseResolves"),
    "M31_replacement_waits_on_process": (
        "ReplacementWaits",
        "ExpiredReplacementNeverWaits",
    ),
    "M32_release_while_swarming": ("ReleaseGuard", "StopResolves"),
    "M33_no_stale_from_fixed": ("StaleFromFixed", "UnreadableFixedReachesStale"),
    "M34_countermeasures_never_fix": ("FailForever", "StopResolves"),
}

# witness config -> the property it violates: the state it names is reachable
WITNESSES = {
    "W1_hand_fix_reaches_cleared": "HandFixedNeverClears",
    "W2_countermeasure_reaches_cleared": "NeverClearedByCountermeasure",
}

# design_live differs from design only in exploration bounds
LIVE_BOUNDS = {"Occurrences", "MaxGen", "Modes"}

ROW = re.compile(
    r"(\S+) cfg_sha256=([0-9a-f]{64}) out_sha256=([0-9a-f]{64}) result=(.+)"
)


def _base(prop: str) -> str:
    return DESIGN_LIVE if prop in TEMPORAL else DESIGN


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(r"model: AndonStop\.tla sha256=([0-9a-f]{64})", lines[0])
    assert model, lines[0]
    rows: dict[str, tuple[str, str, str]] = {}
    for line in lines[1:]:
        m = ROW.fullmatch(line)
        assert m, line
        rows[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    return model.group(1), rows


def _out(name: str) -> str:
    return (MODEL_DIR / "results" / f"{name}.out").read_text()


def _cfg(name: str) -> str:
    return (MODEL_DIR / f"{name}.cfg").read_text()


def _constants(name: str) -> dict[str, str]:
    block = _cfg(name).split("CONSTANTS", 1)[1].split("CHECK_DEADLOCK", 1)[0]
    return dict(re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", block, re.MULTILINE))


def _checked(name: str) -> set[str]:
    return set(
        re.findall(r"^(?:INVARIANT|PROPERTY)\s+(\w+)\s*$", _cfg(name), re.MULTILINE)
    )


def _differs(a: dict[str, str], b: dict[str, str]) -> set[str]:
    return {k for k in a if b.get(k) != a[k]}


def _violation_line(prop: str) -> str:
    if prop in TEMPORAL:
        return "Temporal properties were violated"
    if prop in ACTION:
        return f"Action property {prop} is violated"
    return f"Invariant {prop} is violated"


@pytest.mark.unit
def test_results_are_for_the_committed_model_configs_and_outputs() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL)
    assert set(rows) == {DESIGN, DESIGN_LIVE, *MUTANTS, *WITNESSES}
    on_disk = {p.stem for p in MODEL_DIR.glob("*.cfg")}
    assert on_disk == set(rows)
    for name, (cfg_sha, out_sha, _) in rows.items():
        assert cfg_sha == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert out_sha == _sha(MODEL_DIR / "results" / f"{name}.out"), name


@pytest.mark.unit
def test_design_passes_every_property() -> None:
    _, rows = _summary()
    assert _checked(DESIGN) == {"TypeOK", *INVARIANTS, *ACTION}
    assert _checked(DESIGN_LIVE) == set(TEMPORAL)
    for name in (DESIGN, DESIGN_LIVE):
        assert rows[name][2] == "PASS", name
        out = _out(name)
        assert "No error has been found" in out, name
        assert out.rstrip().endswith("exit=0"), name


@pytest.mark.unit
def test_design_live_differs_from_design_only_in_exploration_bounds() -> None:
    design = _constants(DESIGN)
    live = _constants(DESIGN_LIVE)
    assert set(design) == set(live)
    assert _differs(design, live) == LIVE_BOUNDS


@pytest.mark.unit
def test_every_property_has_a_mutant() -> None:
    assert {target for _, target in MUTANTS.values()} == {
        *INVARIANTS,
        *ACTION,
        *TEMPORAL,
    }


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_property_fails_on_its_one_knob_mutation(name: str) -> None:
    knob, prop = MUTANTS[name]
    _, rows = _summary()
    assert _differs(_constants(_base(prop)), _constants(name)) == {knob}
    assert _checked(name) == {prop}
    assert rows[name][2] == f"VIOLATED {prop}"
    out = _out(name)
    assert re.search(r"^exit=[1-9][0-9]*$", out, re.MULTILINE)
    assert "No error has been found" not in out
    assert _violation_line(prop) in out


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(WITNESSES))
def test_each_witness_reaches_its_state(name: str) -> None:
    prop = WITNESSES[name]
    _, rows = _summary()
    assert _differs(_constants(DESIGN), _constants(name)) == set()
    assert _checked(name) == {prop}
    assert rows[name][2] == f"VIOLATED {prop}"
    assert f"Invariant {prop} is violated" in _out(name)


@pytest.mark.unit
def test_a_hand_fixed_stop_reaches_cleared_through_the_readings() -> None:
    # the witness trace takes hand_fix_recorded and ends in cleared; the liveness
    # check (design_live) ranges over every such path, so a hand-fixed stop is in it
    trace = _out("W1_hand_fix_reaches_cleared")
    assert "HandFixRecorded" in trace
    assert re.search(r"^/\\ st = \"cleared\"$", trace, re.MULTILINE)
    readings = _constants(DESIGN)["ConsecutiveFixedReadings"]
    assert re.search(rf"^/\\ goodPost = {readings}$", trace, re.MULTILINE)
    assert "HandFixedNeverClears" in _checked("W1_hand_fix_reaches_cleared")


# ---------------------------------------------------------------------------
# The contract comparison: the one link between the model and the deployment.
# ---------------------------------------------------------------------------

STOP_NODE = "node_andon_stop_reducer"
LEASE_NODE = "node_lane_lease_reducer"

# model constant -> field of config.andon_stop
CONFIG_FIELDS = {
    "MaxLeaseReplacements": "max_lease_replacements",
    "ConsecutiveFixedReadings": "consecutive_fixed_readings",
    "StaleAfterHours": "stale_after_hours",
    "StormDistinctKeys": "storm_distinct_keys",
    "CanaryDispatchesPerReadbackInterval": "canary_dispatches_per_readback_interval",
    "RecoveryMinimumCrossings": "recovery_minimum_crossings",
}

Machine = tuple[set[str], set[str], set[str], set[tuple[str, str, str]]]


def _contract(node: str, fixture: str) -> dict[str, Any]:
    found = sorted(REPO.glob(f"src/*/nodes/{node}/contract.yaml"))
    path = found[0] if found else FIXTURES / fixture
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _stop_contract() -> dict[str, Any]:
    return _contract(STOP_NODE, "andon_stop_contract_fragment.yaml")


def _lease_contract() -> dict[str, Any]:
    return _contract(LEASE_NODE, "lane_lease_contract_fragment.yaml")


def _tla_block(tla: str, name: str) -> str:
    m = re.search(rf"^{name} ==\s*\{{(.*?)\}}[ \t]*$", tla, re.MULTILINE | re.DOTALL)
    assert m, name
    return m.group(1)


def _tla_machine(tla: str, prefix: str) -> Machine:
    """States, terminal states, triggers and transitions the model declares."""
    states = set(re.findall(r'"([^"]+)"', _tla_block(tla, f"{prefix}States")))
    terminal_name = "Terminal" if prefix == "Stop" else f"{prefix}Terminal"
    terminal = set(re.findall(r'"([^"]+)"', _tla_block(tla, terminal_name)))
    triggers = set(re.findall(r'"([^"]+)"', _tla_block(tla, f"{prefix}Triggers")))
    transitions = set(
        re.findall(
            r'<<"([^"]+)", "([^"]+)", "([^"]+)">>',
            _tla_block(tla, f"{prefix}Transitions"),
        )
    )
    return states, terminal, triggers, transitions


def _contract_machine(contract: dict[str, Any]) -> Machine:
    sm = contract["state_machine"]
    states = {s["state_name"] for s in sm["states"]}
    terminal = set(sm["terminal_states"])
    triggers = {t["trigger"] for t in sm["transitions"]}
    transitions = {
        (t["from_state"], t["trigger"], t["to_state"]) for t in sm["transitions"]
    }
    return states, terminal, triggers, transitions


def _machine_differences(tla: str, prefix: str, contract: dict[str, Any]) -> list[str]:
    model = _tla_machine(tla, prefix)
    declared = _contract_machine(contract)
    labels = ("states", "terminal states", "triggers", "transitions")
    return [
        f"{label}: model-only {sorted(m - c)}, contract-only {sorted(c - m)}"
        for label, m, c in zip(labels, model, declared, strict=True)
        if m != c
    ]


def _constant_differences(
    constants: dict[str, str], config: dict[str, Any]
) -> list[str]:
    return [
        f"{name}: cfg {constants.get(name)} contract {config[field]}"
        for name, field in CONFIG_FIELDS.items()
        if constants.get(name) != str(config[field])
    ]


def _tla() -> str:
    return (MODEL_DIR / MODEL).read_text()


@pytest.mark.unit
def test_the_stop_machine_is_the_stop_contract() -> None:
    assert _machine_differences(_tla(), "Stop", _stop_contract()) == []


@pytest.mark.unit
def test_the_lease_machine_is_the_lease_contract() -> None:
    assert _machine_differences(_tla(), "Lease", _lease_contract()) == []


@pytest.mark.unit
def test_the_stop_reopens_from_fixed_on_a_mismatched_readback() -> None:
    edge = ("fixed", "readback_mismatch", "open")
    assert edge in _tla_machine(_tla(), "Stop")[3]
    assert edge in _contract_machine(_stop_contract())[3]


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted({DESIGN, DESIGN_LIVE, *MUTANTS, *WITNESSES}))
def test_every_cfg_constant_is_the_contract_value(name: str) -> None:
    config = _stop_contract()["config"]["andon_stop"]
    assert _constant_differences(_constants(name), config) == []


@pytest.mark.unit
def test_the_comparison_fails_when_a_state_name_differs() -> None:
    tla = _tla().replace('"swarming"', '"swarm"')
    assert _machine_differences(tla, "Stop", _stop_contract())


@pytest.mark.unit
def test_the_comparison_fails_when_a_trigger_name_differs() -> None:
    tla = _tla().replace('"lease_granted"', '"lease_given"')
    assert _machine_differences(tla, "Stop", _stop_contract())
    tla = _tla().replace('"process_found"', '"process_seen"')
    assert _machine_differences(tla, "Lease", _lease_contract())


@pytest.mark.unit
def test_the_comparison_fails_when_a_transition_is_dropped() -> None:
    tla = _tla().replace('  <<"fixed", "readback_mismatch", "open">>,\n', "")
    assert _machine_differences(tla, "Stop", _stop_contract())


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(CONFIG_FIELDS))
def test_the_comparison_fails_when_a_cfg_constant_differs(name: str) -> None:
    config = dict(_stop_contract()["config"]["andon_stop"])
    config[CONFIG_FIELDS[name]] = int(config[CONFIG_FIELDS[name]]) + 1
    assert _constant_differences(_constants(DESIGN), config)


@pytest.mark.unit
def test_every_transition_of_each_machine_is_fired_by_an_action() -> None:
    tla = _tla()
    for prefix, call in (("Stop", "Step"), ("Lease", "LStep")):
        for _, trigger, to in _tla_machine(tla, prefix)[3]:
            fired = re.search(rf'\b{call}\([^()]*"{trigger}", "{to}"\)', tla)
            assert fired, (prefix, trigger, to)


# (rule of the stop's guards and recovery rules, operator that carries it): each is a
# property, checked by a design config, or an action of Next.
RULES = {
    "5.4 a stop refuses new dispatch only": "StopRefusesNewDispatchOnly",
    "5.4 the swarm lane is never refused by its own stop": "SwarmLaneNeverRefused",
    "5.4 an exemption is valid only when the dispatcher binds it": "OnlyBoundExemptionsAdmitted",
    "5.4 new dispatch is refused while a stop is open": "NewDispatchRefusedWhileOpen",
    "5.4 a readback that reads UNKNOWN holds the stop": "UnknownNeverClears",
    "5.4 a stop open past the stale limit with no live lane becomes a decision row": "StaleLimitReached",
    "5.4 a stale stop becomes one decision row": "StaleEmitsDecisionRow",
    "5.4 a stale stop never clears itself": "StaleNeverClears",
    "5.4 a storm of keys opens one stop": "StormIsOneStop",
    "5.4 two detectors for one key make one stop": "OneStopPerKey",
    "5.4 production promotion is untouched": "StopRefusesNewDispatchOnly",
    "5.5 no human clears a stop": "NoHumanClear",
    "5.5 a human can only declare false, recorded": "DeclaredFalseIsRecorded",
    "5.5 a stop never clears without a reading": "NeverClearsWithoutReadings",
    "5.5 the readback counts only crossings after countermeasure_at": "OnlyPostCountermeasureCounts",
    "5.5 the gate admits a proof crossing while a stop is open": "CanaryAdmittedWhileOpen",
    "5.5 an expired lease is replaced once per expiry, at most the budget": "ReplacementsBounded",
    "5.5 a restart loses no open stop": "GateSeesOpenStop",
    "5.5 a gate in observe mode never refuses": "ObserveNeverRefuses",
    "5.8 a contract migration never enters a terminal state": "MigrationNeverEntersTerminal",
    "5.8 a hand fix needs hand_fix_allowed": "HandFixOnlyFirstWithEvidence",
    "5.8 a hand fix anchors countermeasure_at": "FixedHasAnchor",
    "5.8 a fixed stop unreadable for the stale limit reaches stale": "UnreadableFixedReachesStale",
    "0d.7 at most one active lease per key": "AtMostOneActiveLease",
    "0d.7 a write needs an active lease": "WriteOnlyUnderActiveLease",
    "0d.7 a write carries the current generation": "StaleGenerationWriteRefused",
    "0d.7 a crash leaves the lease recoverable": "PendingLeaseResolves",
    "0d.7 a replacement never waits on an expired process": "ExpiredReplacementNeverWaits",
    "0d.7 an unreadable start never becomes active unreconciled": "ActiveRequiresStartReceipt",
    "5.5 every open stop eventually clears or reaches a decision row": "StopResolves",
}


@pytest.mark.unit
@pytest.mark.parametrize("rule", sorted(RULES))
def test_each_guard_and_recovery_rule_is_a_property_or_an_action(rule: str) -> None:
    name = RULES[rule]
    tla = _tla()
    assert re.search(rf"^{name} ==", tla, re.MULTILINE), rule
    checked = _checked(DESIGN) | _checked(DESIGN_LIVE)
    in_next = re.search(
        rf"\\/ .*\b{name}\b", tla.split("Next ==", 1)[1].split("Fairness", 1)[0]
    )
    assert name in checked or in_next, rule


@pytest.mark.unit
def test_the_model_names_no_store_bus_host_or_path() -> None:
    deny = re.compile(
        r"\b(store|stores|bus|host|hosts|path|paths|repository|repositories|"
        r"organization|organisation|kafka|redis|postgres|sqlite|http|https)\b",
        re.IGNORECASE,
    )
    texts = [_tla()] + [p.read_text() for p in sorted(MODEL_DIR.glob("*.cfg"))]
    for text in texts:
        assert deny.search(text) is None, deny.search(text)
    assert "/" not in "".join(p.read_text() for p in MODEL_DIR.glob("*.cfg"))


@pytest.mark.unit
def test_verdict_names_model_digest() -> None:
    verdict = (MODEL_DIR / "VERDICT.txt").read_text()
    lines = re.findall(r"^verdict: .*$", verdict, re.MULTILINE)
    assert len(lines) == 1, f"VERDICT.txt needs exactly one verdict line, has {lines}"
    approved = re.fullmatch(
        r"verdict: (APPROVE|REJECT) model=AndonStop\.tla sha256=([0-9a-f]{64})",
        lines[0],
    )
    assert approved, lines[0]
    assert approved.group(2) == _sha(MODEL_DIR / MODEL)
    assert approved.group(1) == "APPROVE"
