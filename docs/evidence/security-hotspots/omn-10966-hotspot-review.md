# Security Hotspot Review — OMN-10966

**Date:** 2026-05-17
**Reviewer:** jonahgabriel
**Ticket:** OMN-10966
**Scope:** omnimarket codebase — all SonarCloud security hotspots

---

## Summary

Three security hotspot categories identified. All reviewed below with rationale.
Two receive `# NOSONAR` suppression with inline rationale.

---

## Hotspot 1 — S7503: Unnecessary async function

**Files:**
- `src/omnimarket/nodes/node_delegation_orchestrator/plugin.py:99` (`initialize`)
- `src/omnimarket/nodes/node_delegation_orchestrator/plugin.py:112` (`validate_handshake`)

**Rule:** S7503 — function declared `async` but never uses `await`.

**Decision:** SAFE — suppressed with `# NOSONAR S7503`

**Rationale:**
Both methods implement `ProtocolDomainPlugin` from `omnibase_infra`, which declares
these methods as `async def`. The protocol signature cannot be changed without breaking
all implementations across the runtime. Removing `async` from this specific implementation
would violate the interface contract. These are intentional no-op implementations for
the delegation plugin, which has no external resource dependencies. SonarCloud does not
model protocol/interface inheritance here; the `async` keyword is mandated by the
contract, not a mistake.

**Fix applied:** `# NOSONAR S7503: protocol-required async (ProtocolDomainPlugin); no await needed in no-op implementation` added inline to both function signatures.

---

## Hotspot 2 — S2245: Weak random number generator

**Files:**
- `src/omnimarket/nodes/node_model_router/handlers/handler_model_router.py:262` — `random.random()` for backoff jitter
- `src/omnimarket/nodes/node_adr_canary_orchestrator/handlers/handler_canary_orchestrator.py:631` — `random.choices()` for run ID suffix

**Rule:** S2245 — use of `random` module (not cryptographically secure).

**Decision:** SAFE — no fix required

**Rationale (model_router):**
`random.random()` is used to compute jitter in an exponential backoff delay:
`jitter = base * _BACKOFF_JITTER * (2 * random.random() - 1)`. This is a
performance/retry strategy, not a security or authentication context. The jitter
value affects only how long the code sleeps before retrying an LLM call.
Predictability of this value has zero security impact. Using `secrets.SystemRandom`
for this purpose would be wasteful and misleading.

**Rationale (canary_orchestrator):**
`random.choices()` generates a 6-character alphanumeric suffix for a run ID used in
internal evidence file naming: `f"{ts}-{suffix}"`. This run ID is a developer-facing
trace handle stored in local files, not used for authentication, authorization, session
management, or any security boundary. Collision risk with a 6-char suffix over a
timestamp is negligible for its purpose. Cryptographic randomness is not required or
appropriate here.

---

## Hotspot 3 — S2076: OS command injection via shell=True

**File:**
- `src/omnimarket/nodes/node_dod_verify/services/evidence_collector.py:521-528`

**Rule:** S2076 — `subprocess.run()` called with `shell=True` and a variable command string.

**Decision:** SAFE — accepted risk, design-level justification

**Rationale:**
The `evidence_collector.py` is a DoD verification tool that executes commands declared
in contract YAML files authored by developers on the omninode team. The `cmd_str` is
read from `check["command"]` or `check["check_value"]` in a YAML contract file that
lives in the repository under version control. This is equivalent to a `Makefile` or
CI workflow step — the attack surface is: a malicious developer could craft a contract
YAML with a destructive command. This is accepted because:

1. Contract YAML files are committed to version control and reviewed via PR.
2. The tool runs in developer local environments and CI, not as an internet-facing service.
3. The execution context is a sandboxed CI runner or the developer's own machine.
4. The payload source is the same trust boundary as `Makefile`, `pyproject.toml`
   scripts, or GitHub Actions workflow steps — all of which also execute arbitrary
   commands.

Switching to `shell=False` with `shlex.split()` would break commands that use shell
features (pipes, redirections, variable expansion) that are legitimately used in
contract evidence checks. The `cwd` containment check (`_resolve_cwd`) and placeholder
validation already limit the execution context.

No code change required. Risk accepted as developer-tooling design intent.

---

## S7503 Coverage Scan — Other Files

A broader scan of the omnimarket codebase found 168 `async def` functions without
`await`. These are all protocol stub implementations, test doubles, or interface
adapters — the same pattern as the delegation plugin. SonarCloud flags are scoped to
the files changed in the PR/analysis window; this review covers the delegation plugin
findings which are the reported S7503 instances for OMN-10966. The broader pattern
is architectural (ONEX protocols require async signatures) and is not a defect.

---

## Conclusion

| Hotspot | Rule | Decision | Action |
|---------|------|----------|--------|
| `initialize` in plugin.py | S7503 | SAFE | `# NOSONAR` added |
| `validate_handshake` in plugin.py | S7503 | SAFE | `# NOSONAR` added |
| `random.random()` in handler_model_router.py | S2245 | SAFE | No change needed |
| `random.choices()` in handler_canary_orchestrator.py | S2245 | SAFE | No change needed |
| `shell=True` in evidence_collector.py | S2076 | SAFE (accepted risk) | No change needed |
