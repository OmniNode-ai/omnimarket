# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure readers of what one customer-local delegation run left behind (OMN-26043).

The egress instrument is ``strace -f -e trace=connect``; the run's evidence is the three files ``onex delegate`` writes
(``result.txt``, ``receipt.json``, ``run.json``) and the install's own origin records. Everything here takes text or parsed
JSON and returns facts; nothing opens a socket or a file, so the readers are tested on recorded traces and receipts.

A connect the parser cannot read is graded external: an unreadable address is not a proven-local one.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: The distributions a delegation imports; each must have come from the package index.
ONEX_DISTRIBUTIONS: Final[tuple[str, ...]] = (
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omnimarket",
)
#: The three files a local delegation writes.
RUN_FILES: Final[tuple[str, ...]] = ("result.txt", "receipt.json", "run.json")
#: The model server's own generated-token counters, the first present is read.
TOKEN_COUNTERS: Final[tuple[str, ...]] = (
    "llamacpp:tokens_predicted_total",
    "vllm:generation_tokens_total",
)

KIND_MODEL: Final = "model"
KIND_EXTERNAL: Final = "external"
KIND_NAME_LOOKUP: Final = "name_lookup"

_INET_RE: Final = re.compile(
    r"sa_family=AF_INET, sin_port=htons\((?P<port>\d+)\), "
    r'sin_addr=inet_addr\("(?P<host>[^"]+)"\)'
)
_INET6_RE: Final = re.compile(
    r"sa_family=AF_INET6, sin6_port=htons\((?P<port>\d+)\).*?"
    r'inet_pton\(AF_INET6, "(?P<host>[^"]+)"'
)
_UNIX_RE: Final = re.compile(r'sa_family=AF_UNIX, sun_path=@?"(?P<path>[^"]*)"')
_FAMILY_RE: Final = re.compile(r"sa_family=(?P<family>AF_[A-Z0-9]+)")


@dataclass(frozen=True)
class Connect:
    family: str
    host: str | None
    port: int | None
    path: str | None
    kind: str

    def label(self) -> str:
        where = f"{self.host}:{self.port}" if self.host else (self.path or self.family)
        return f"{self.kind}:{where}"


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _is_model_host(host: str, model_host: str) -> bool:
    address, declared = _ip(host), _ip(model_host)
    if address is None or declared is None:
        return False
    if declared.is_loopback:
        return address.is_loopback
    return address == declared


def classify(
    family: str,
    host: str | None,
    port: int | None,
    path: str | None,
    *,
    model_host: str,
    model_port: int,
) -> str:
    """What a connect was for: the model, an external address, a name lookup, or something local."""
    if family in ("AF_INET", "AF_INET6") and host is not None:
        if port == model_port and _is_model_host(host, model_host):
            return KIND_MODEL
        address = _ip(host)
        if address is None or not address.is_loopback:
            return KIND_EXTERNAL
        return KIND_NAME_LOOKUP if port == 53 else "loopback_other"
    if family == "AF_UNIX":
        # glibc asks nscd before it asks DNS; a connect to its socket is a name lookup whether or not nscd runs.
        return KIND_NAME_LOOKUP if path is not None and "nscd" in path else "unix_local"
    return "other_family"


def parse_connects(
    strace_text: str, *, model_host: str, model_port: int
) -> tuple[Connect, ...]:
    """Every connect() of a ``strace -f -e trace=connect`` log, classified; ``resumed`` lines carry no address and are skipped."""
    connects: list[Connect] = []
    for line in strace_text.splitlines():
        if "connect(" not in line or "resumed>" in line:
            continue
        family_match = _FAMILY_RE.search(line)
        if family_match is None:
            continue
        family = family_match.group("family")
        regex = {"AF_INET": _INET_RE, "AF_INET6": _INET6_RE, "AF_UNIX": _UNIX_RE}.get(
            family
        )
        found = regex.search(line) if regex else None
        unix = family == "AF_UNIX"
        host = found.group("host") if found and not unix else None
        port = int(found.group("port")) if found and not unix else None
        path = found.group("path") if found and unix else None
        if family in ("AF_INET", "AF_INET6") and host is None:
            connects.append(Connect(family, None, None, None, KIND_EXTERNAL))
            continue
        kind = classify(
            family, host, port, path, model_host=model_host, model_port=model_port
        )
        connects.append(Connect(family, host, port, path, kind))
    return tuple(connects)


def of_kind(connects: Sequence[Connect], *kinds: str) -> tuple[Connect, ...]:
    return tuple(c for c in connects if c.kind in kinds)


def tokens_from_metrics(text: str) -> int | None:
    """The sum of the first token counter a Prometheus exposition carries; None when the server exposes none."""
    for counter in TOKEN_COUNTERS:
        samples = [
            line
            for line in text.splitlines()
            if line.startswith(counter)
            and line[len(counter) : len(counter) + 1] in ("{", " ")
        ]
        if samples:
            return sum(int(float(line.split()[-1])) for line in samples)
    return None


def load_json(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def no_checkout_findings(
    receipt: Any,
    direct_urls: Mapping[str, Any],
    source_trees: Sequence[str] | None,
    git_ancestor: str | None,
) -> tuple[str, ...]:
    """Why the machine is not proven a no-checkout one; empty when every distribution came from the package index."""
    identity = _mapping(
        _mapping(_mapping(receipt).get("receipt")).get("runtime_identity")
    )
    if not identity:
        return (
            "the receipt carries no runtime identity, so where the code came from is unproven",
        )
    findings: list[str] = []
    packages = _mapping(identity.get("packages"))
    for dist in ONEX_DISTRIBUTIONS:
        entry = packages.get(dist)
        if not isinstance(entry, dict):
            findings.append(f"{dist} is absent from the runtime identity")
            continue
        if entry.get("source") != "registry":
            findings.append(
                f"{dist} source is {entry.get('source')!r}, not the package index"
            )
        if entry.get("commit") or entry.get("import_path"):
            findings.append(
                f"{dist} names a commit or import path, which only a checkout has"
            )
        if dist not in direct_urls:
            findings.append(f"no install-origin readback for {dist}")
        elif direct_urls[dist] is not None:
            findings.append(
                f"{dist} was installed from {direct_urls[dist]!r}, not the index"
            )
    if source_trees is None:
        findings.append("no source-tree scan was recorded")
    elif source_trees:
        findings.append(f"OmniNode source tree(s) on the machine: {list(source_trees)}")
    if git_ancestor:
        findings.append(
            f"the working directory is inside a git work tree at {git_ancestor}"
        )
    return tuple(findings)


def three_files_findings(
    files: Mapping[str, str | None],
    stdout_doc: Any,
    receipt: Any,
    configured_rc: int | None,
) -> tuple[str, ...]:
    """Why result.txt, receipt.json and run.json do not agree; empty when the three files exist, parse and agree."""
    findings: list[str] = []
    for name in RUN_FILES:
        content = files.get(name)
        if content is None:
            findings.append(f"{name} is missing")
        elif not content.strip():
            findings.append(f"{name} is empty")
    run_doc = load_json(files.get("run.json"))
    if files.get("receipt.json") is not None and not isinstance(receipt, dict):
        findings.append("receipt.json does not parse as a JSON object")
    if files.get("run.json") is not None and not isinstance(run_doc, dict):
        findings.append("run.json does not parse as a JSON object")
    if not isinstance(stdout_doc, dict):
        findings.append("stdout was not exactly one JSON result")
    if findings:
        return tuple(findings)
    for field in ("correlation_id", "run_id"):
        seen = {
            "stdout": stdout_doc.get(field),
            "receipt": receipt.get(field),
            "run": run_doc.get(field),
        }
        if len(set(seen.values())) != 1 or None in seen.values():
            findings.append(f"{field} disagrees across stdout and the files: {seen}")
    response = _mapping(_mapping(_mapping(receipt).get("receipt")).get("result")).get(
        "response"
    )
    text = files.get("result.txt") or ""
    if not isinstance(response, str) or not response.strip():
        findings.append("the receipt carries no accepted response")
    elif text.strip() != response.strip():
        findings.append("result.txt is not the response the receipt accepted")
    if configured_rc != 0:
        findings.append(f"onex delegate exited {configured_rc}, not 0")
    if receipt.get("status") != "success":
        findings.append(
            f"the receipt status is {receipt.get('status')!r}, not 'success'"
        )
    return tuple(findings)


@dataclass(frozen=True)
class ReceiptFacts:
    tier: str | None
    model: str | None
    endpoint: str | None
    attempt_tiers: tuple[str, ...]
    accepted_last: bool
    escalated: bool
    cost_usd: float
    versions: dict[str, str]


def receipt_facts(receipt: Any) -> ReceiptFacts:
    doc = _mapping(receipt)
    inner = _mapping(doc.get("receipt"))
    result = _mapping(inner.get("result"))
    attempts = [a for a in (result.get("attempts") or []) if isinstance(a, dict)]
    cost = _mapping(result.get("metrics")).get("cost_usd")
    packages = _mapping(_mapping(inner.get("runtime_identity")).get("packages"))
    return ReceiptFacts(
        tier=doc.get("routing_tier")
        if isinstance(doc.get("routing_tier"), str)
        else None,
        model=doc.get("model") if isinstance(doc.get("model"), str) else None,
        endpoint=doc.get("endpoint") if isinstance(doc.get("endpoint"), str) else None,
        attempt_tiers=tuple(str(a.get("tier")) for a in attempts),
        accepted_last=bool(attempts)
        and attempts[-1].get("acceptance_decision") == "accept",
        escalated=bool(result.get("escalation_count")),
        cost_usd=float(cost) if isinstance(cost, int | float) else 0.0,
        versions={
            str(name): str(entry.get("version"))
            for name, entry in packages.items()
            if isinstance(entry, dict) and entry.get("version")
        },
    )
