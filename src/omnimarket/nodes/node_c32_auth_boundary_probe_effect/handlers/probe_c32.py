# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""C32 probe -- the local dashboard's auth boundary (OMN-19989, OMN-19984).

Moved from the cloud repository's producer script into this node (OMN-23213); the grading is unchanged.
The node's real prober calls :func:`run`; nothing here reads GitHub.

WHAT C32 CLAIMS
    "The local dashboard answers only on loopback and only to a token minted at
    start, and no provider secret value ever reaches a page."

WHY IT IS ITS OWN CRITERION
    A loopback bind ALONE, which is how OMN-19916 was first written, lets any
    local process -- or a DNS-rebinding page in the developer's own browser --
    read their prompts and answers. Plan decision D2 chose the bind plus a token
    minted per start. That is a security boundary, not a rendering property, so
    it is graded on its own exit code: folded into C31, a page-layout regression
    and a credential leak would move one chip together.

HOW IT RUNS
    The way a developer runs it, in a scratch HOME: `onex local init`, one
    fixture delegation seeded through the production projection handler (the
    C31 fixture), a provider key planted with `onex secret set`, then the real
    `onex dashboard` started TWICE on one loopback port. The first start's token
    is the stale one; the second start is the one graded.

THE FIVE CASES, AND WHY CASE 1 IS NOT OPTIONAL
    1. loopback + current token              -> served
    2. loopback + stale token                -> refused
    3. loopback + no token                   -> refused
    4. non-loopback interface                -> refused
    5. cross-origin read                     -> refused

    Case 1 is the POSITIVE CONTROL. It holds only when the start URL carried a
    token, both protected reads answered 2xx, and the decisions exposure served
    the fixture's own correlation id. Four refusals from a process serving
    nothing prove nothing, so a refusal is a 401 or 403 the server chose to send:
    silence, a 404 or a 5xx is never read as one, and a dead server fails every
    case.

    What each case sends, against the catalogue and the decisions exposure:
      * the token travels every way a start URL can hand it to a browser: the
        URL's own query, a bearer header when a `token` parameter is in the query
        or fragment, and any cookie the server set when the start URL was opened.
        Cases 1, 2 and 4 present the same channels, so the stale and interface
        refusals are attributable to the token and the interface alone.
      * case 2 replays the FIRST start's URL against the second. A start URL with
        no token, or a token that did not change between starts, fails it.
      * case 4 sends the current token to this machine's non-loopback address on
        the dashboard's port. Nothing answering, or a refusal, holds; a machine
        with no non-loopback address fails the case rather than skipping it. The
        start-time half -- a non-loopback `dashboard.bind` refused at start -- is
        graded as its own clause below.
      * case 5 is two reads, both of which must be refused: a cross-origin read
        with the current token (refused when the server answers 401/403, or 2xx
        with no Access-Control-Allow-Origin that admits the origin), and a
        DNS-rebinding read, `Host` naming a foreign name and no token, as a
        rebinding page would send it (refused when answered 400, 401, 403 or 421).
        A browser blocks a plain cross-origin read with no CORS header by itself;
        the rebinding read is the one the D2 token exists to stop.

CREDENTIAL HANDLING
    The planted value is generated here, piped to `onex secret set` on stdin
    (never argv), and then SEARCHED FOR: in every response the cases and the page
    reads got, in every file under the scratch HOME (the event log, pending
    credential events) and the dashboard's own output, and in every store table
    except the one credential table that holds it by design (the 2026-09-18
    ruling keeps the value in delegation.sqlite). The raw store file is the
    positive control, the reading the operator gave on OMN-19985 on 2026-10-07:
    a search that cannot find the value where it is proves nothing by its
    absences elsewhere. The value is never echoed, never logged and never in the
    record, only the finding is. While the key is set, every proxy variable
    points at a closed local port, so the provider model lookup `onex secret set`
    makes fails locally and the value never leaves the machine.

STATUS (2026-10-15)
    The per-start token is built in omnimarket (OMN-19916): `onex dashboard`
    prints `http://127.0.0.1:<port>/?token=<token>`, refuses a foreign Host or
    Origin with 403 and a read without this start's token with 401. Until a
    release carries it, the published dashboard mints no token and cases 1, 2,
    3 and 5 FAIL. The plant names `--model` because `onex secret set` refuses
    an OpenRouter key without one from a script since OMN-20844; without it
    every run stopped PRECONDITION-ABSENT before grading a case.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import urllib.parse
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.probe_base import (
    FIXTURE_CORRELATION_ID,
    Clause,
    DashboardStart,
    Precondition,
    ProbeResult,
    _run,
    local_tenant_identity,
    onex_cli,
    onex_subcommand,
    running_dashboard,
    seed_fixture_delegation,
    started_dashboard,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.models.model_c32_probe import (
    DECISIONS_TOPIC,
)

CRITERION = "C32"

#: The case table, as data so the test suite can assert it has not shrunk.
#: `expect_served` True marks the positive control.
AUTH_CASES = (
    ("loopback request carrying the current token", True),
    ("loopback request carrying a stale token", False),
    ("loopback request carrying no token", False),
    ("request arriving on a non-loopback interface", False),
    ("cross-origin read from a page the developer did not open", False),
)
POSITIVE_CONTROL = AUTH_CASES[0][0]
SECRET_CLAUSE = (
    "a planted provider credential value is absent from the event log, the store "
    "and every page response"
)

CATALOGUE_PATH = "/projections"
#: What a token refusal is. Anything else -- no answer, 404, 5xx -- is not one.
REFUSAL_STATUSES = frozenset({401, 403})
#: A refused foreign Host may also be a 400 or a 421 Misdirected Request.
HOST_REFUSAL_STATUSES = frozenset({400, 401, 403, 421})
CROSS_ORIGIN = "http://c32-cross-origin.invalid"
REBIND_HOST = "c32-rebind.invalid"
#: Start-URL parameters read as the token when sent as a bearer header.
TOKEN_PARAMS = ("token", "access_token", "auth_token")
#: A provider the catalogue offers, so the key takes the registration path the
#: Credentials page projects -- not a plain stored string.
PLANT_REF = "llm.openrouter.api_key"
PLANT_PREFIX = "sk-or-v1-c32planted"
#: OpenRouter is a provider where the customer names the model, and since
#: OMN-20844 `onex secret set` refuses a key with none from a script
#: (BYOK_MODEL_NOT_CHOSEN). With the network closed the provider's list cannot
#: be read, so the named model is stored unchecked; it is never called, so the
#: name is the probe's own and no real model id is written here.
PLANT_MODEL = "c32-probe/planted-model"
REQUEST_TIMEOUT_SECONDS = 5
MAX_BODY_BYTES = 8 * 1024 * 1024
_PROXY_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
)
_TOPIC = re.compile(r"onex\.snapshot\.projection\.[A-Za-z0-9_.\-]+")


@dataclass(frozen=True)
class Answer:
    """One HTTP answer, reduced to what the cases grade. `status` None: nothing answered."""

    status: int | None
    allow_origin: str | None = None
    body: str = ""
    set_cookies: tuple[str, ...] = ()

    @property
    def served(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


Request = Callable[[str, dict[str, str]], Answer]


def http_request(url: str, headers: dict[str, str]) -> Answer:
    """GET `url` with exactly `headers` (a `Host` among them is sent as given)."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(
        parts.hostname or "", parts.port, timeout=REQUEST_TIMEOUT_SECONDS
    )
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request("GET", target, headers=headers)
        response = conn.getresponse()
        body = response.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        cookies = tuple(
            c.split(";", 1)[0].strip()
            for c in (response.headers.get_all("Set-Cookie") or [])
        )
        return Answer(
            response.status,
            response.getheader("Access-Control-Allow-Origin"),
            body,
            cookies,
        )
    except (OSError, http.client.HTTPException):
        return Answer(None)
    finally:
        conn.close()


@dataclass(frozen=True)
class Credential:
    """What a start URL hands a browser. Empty means nothing was minted."""

    query: str = ""
    bearer: str | None = None
    cookies: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return bool(self.query or self.bearer or self.cookies)

    def headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.bearer:
            out["Authorization"] = f"Bearer {self.bearer}"
        if self.cookies:
            out["Cookie"] = "; ".join(self.cookies)
        return out

    def url(self, url: str) -> str:
        if not self.query:
            return url
        return f"{url}{'&' if '?' in url else '?'}{self.query}"


def credential_from_start(printed_url: str | None, request: Request) -> Credential:
    """The credential one start handed out: its URL's query, a token parameter, its cookie.

    The start URL is opened once, as a browser opening it would, to collect any
    cookie the server trades the token for. The fragment is never sent.
    """
    if not printed_url:
        return Credential()
    parts = urllib.parse.urlsplit(printed_url)
    params = urllib.parse.parse_qsl(parts.query) + urllib.parse.parse_qsl(
        parts.fragment
    )
    bearer = next((v for k, v in params if k in TOKEN_PARAMS and v), None)
    visit = request(
        urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path or "/", parts.query, "")
        ),
        {},
    )
    return Credential(parts.query, bearer, visit.set_cookies)


def protected_paths(correlation_id: str) -> tuple[str, str]:
    """The catalogue and the exposure that carries a developer's runs."""
    topic = urllib.parse.quote(DECISIONS_TOPIC, safe="")
    return (
        CATALOGUE_PATH,
        f"/projection/{topic}?correlation_id={urllib.parse.quote(correlation_id, safe='')}",
    )


@dataclass(frozen=True)
class Observations:
    """Every answer the five cases got, keyed by protected path."""

    current: Credential
    stale: Credential
    served: dict[str, Answer]
    stale_answers: dict[str, Answer]
    no_token: dict[str, Answer]
    lan_address: str | None
    lan: dict[str, Answer]
    cross_origin: dict[str, Answer]
    rebinding: dict[str, Answer]

    def bodies(self) -> list[tuple[str, str]]:
        groups = {
            "current token": self.served,
            "stale token": self.stale_answers,
            "no token": self.no_token,
            "non-loopback": self.lan,
            "cross-origin": self.cross_origin,
            "rebinding": self.rebinding,
        }
        return [
            (f"{group} {path}", a.body)
            for group, answers in groups.items()
            for path, a in answers.items()
        ]


def observe(
    base_url: str,
    current: Credential,
    stale: Credential,
    *,
    request: Request,
    lan_address: str | None,
    correlation_id: str,
) -> Observations:
    """Send the five cases' requests to the running dashboard at `base_url`."""
    port = urllib.parse.urlsplit(base_url).port
    paths = protected_paths(correlation_id)

    def ask(
        cred: Credential, extra: dict[str, str] | None = None, base: str = base_url
    ) -> dict[str, Answer]:
        return {
            p: request(cred.url(base + p), {**cred.headers(), **(extra or {})})
            for p in paths
        }

    rebind = f"{REBIND_HOST}:{port}"
    return Observations(
        current=current,
        stale=stale,
        served=ask(current),
        stale_answers=ask(stale),
        no_token=ask(Credential()),
        lan_address=lan_address,
        lan=ask(current, base=f"http://{lan_address}:{port}") if lan_address else {},
        cross_origin=ask(current, {"Origin": CROSS_ORIGIN}),
        rebinding=ask(Credential(), {"Host": rebind, "Origin": f"http://{rebind}"}),
    )


def _show(answers: dict[str, Answer]) -> str:
    def one(path: str, a: Answer) -> str:
        label = (
            path.split("?", 1)[0]
            .removeprefix("/projection/")
            .removeprefix("onex.snapshot.projection.")
        )
        return f"{label}={'no answer' if a.status is None else a.status}"

    return ", ".join(one(p, a) for p, a in answers.items()) or "nothing sent"


def _refused(
    answers: dict[str, Answer], statuses: frozenset[int] = REFUSAL_STATUSES
) -> bool:
    return bool(answers) and all(a.status in statuses for a in answers.values())


def _unreadable_cross_origin(a: Answer) -> bool:
    if a.status in REFUSAL_STATUSES:
        return True
    return a.served and a.allow_origin not in ("*", CROSS_ORIGIN)


def grade_auth_cases(obs: Observations, *, correlation_id: str) -> list[Clause]:
    """The five cases, in AUTH_CASES order."""
    names = [name for name, _ in AUTH_CASES]
    decisions = protected_paths(correlation_id)[1]
    fixture = correlation_id in obs.served.get(decisions, Answer(None)).body
    minted = obs.current.present
    served_ok = (
        minted
        and bool(obs.served)
        and all(a.served for a in obs.served.values())
        and fixture
    )
    served = Clause(
        names[0],
        served_ok,
        (
            f"start URL carried a token: {'yes' if minted else 'no -- the dashboard minted no token at start'}; "
            f"{_show(obs.served)}; fixture row {correlation_id} served: {'yes' if fixture else 'no'}"
        ),
    )

    if not obs.stale.present:
        why = (
            "the earlier start minted no token, so there was nothing stale to replay; "
        )
    elif obs.stale == obs.current:
        why = "the token did not change between starts, so it is not per-start; "
    else:
        why = ""
    stale = Clause(
        names[1],
        not why and _refused(obs.stale_answers),
        f"{why}{_show(obs.stale_answers)}",
    )

    none = Clause(names[2], _refused(obs.no_token), _show(obs.no_token))

    lan = obs.lan_address
    if lan is None:
        interface = Clause(
            names[3],
            False,
            "no non-loopback address on this machine to send the request from",
        )
    elif ipaddress.ip_address(lan).is_loopback:
        interface = Clause(
            names[3],
            False,
            f"{lan} is a loopback address, not a non-loopback interface",
        )
    else:
        ok = bool(obs.lan) and all(
            a.status is None or a.status in HOST_REFUSAL_STATUSES
            for a in obs.lan.values()
        )
        interface = Clause(
            names[3], ok, f"current token sent to {lan}: {_show(obs.lan)}"
        )

    origin_ok = bool(obs.cross_origin) and all(
        _unreadable_cross_origin(a) for a in obs.cross_origin.values()
    )
    rebind_ok = _refused(obs.rebinding, HOST_REFUSAL_STATUSES)
    acao = (
        ", ".join(sorted({repr(a.allow_origin) for a in obs.cross_origin.values()}))
        or "none"
    )
    origin = Clause(
        names[4],
        origin_ok and rebind_ok,
        (
            f"Origin {CROSS_ORIGIN} with the current token: {_show(obs.cross_origin)} "
            f"(Access-Control-Allow-Origin {acao}); DNS-rebinding read (Host {REBIND_HOST}, no token): "
            f"{_show(obs.rebinding)}"
        ),
    )
    return [served, stale, none, interface, origin]


# ------------------------------------------------------ the planted credential


@dataclass(frozen=True)
class SecretFindings:
    """Where the planted value was found. Locations only; never the value."""

    control_found: bool
    in_responses: tuple[str, ...]
    in_logs: tuple[str, ...]
    in_tables: tuple[str, ...]
    responses_read: int
    files_read: int
    tables_read: int


def grade_planted_secret(f: SecretFindings) -> Clause:
    if not f.control_found:
        return Clause(
            SECRET_CLAUSE,
            False,
            (
                "positive control failed: the search did not find the planted value in the raw "
                "credential store, so its absences elsewhere prove nothing"
            ),
        )
    if not (f.responses_read and f.files_read and f.tables_read):
        return Clause(
            SECRET_CLAUSE,
            False,
            (
                f"searched nothing to search: {f.responses_read} response(s), {f.files_read} file(s), "
                f"{f.tables_read} table(s)"
            ),
        )
    leaks = [
        *(f"response {r}" for r in f.in_responses),
        *(f"file {p}" for p in f.in_logs),
        *(f"table {t}" for t in f.in_tables),
    ]
    return Clause(
        SECRET_CLAUSE,
        not leaks,
        (
            f"searched {f.responses_read} response(s), {f.files_read} file(s) (event log and process "
            f"output) and {f.tables_read} store table(s); positive control found it in the raw store; "
            + ("found nowhere else" if not leaks else "FOUND in " + ", ".join(leaks))
        ),
    )


def store_family(store: Path) -> set[Path]:
    return {
        store,
        *(store.with_name(store.name + s) for s in ("-wal", "-shm", "-journal")),
    }


def raw_store_holds(store: Path, value: str) -> bool:
    needle = value.encode()
    return any(p.is_file() and needle in p.read_bytes() for p in store_family(store))


def scan_files(
    root: Path, value: str, *, exclude: set[Path]
) -> tuple[tuple[str, ...], int]:
    """(files under `root` holding the value, files read), the store family excluded."""
    needle = value.encode()
    skip = {p.resolve() for p in exclude}
    hits: list[str] = []
    read = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file() or path.resolve() in skip:
                continue
            read += 1
            try:
                if needle in path.read_bytes():
                    hits.append(path.relative_to(root).as_posix())
            except OSError:
                hits.append(f"{path.relative_to(root).as_posix()} (unreadable)")
    return tuple(sorted(hits)), read


def scan_tables(
    db: Path, value: str, *, credential_table: str
) -> tuple[tuple[str, ...], int]:
    """(store tables holding the value, tables read), the credential table skipped."""
    needle = value.encode()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        hits: list[str] = []
        read = 0
        for table in tables:
            if table == credential_table:
                continue
            read += 1
            quoted = table.replace('"', '""')
            for row in conn.execute(f'SELECT * FROM "{quoted}"'):
                if any(
                    (isinstance(c, str) and value in c)
                    or (isinstance(c, bytes) and needle in c)
                    for c in row
                ):
                    hits.append(table)
                    break
        return tuple(sorted(hits)), read
    finally:
        conn.close()


def secret_findings(
    *,
    value: str,
    scratch: Path,
    store: Path,
    credential_table: str,
    bodies: list[tuple[str, str]],
) -> SecretFindings:
    in_logs, files_read = scan_files(scratch, value, exclude=store_family(store))
    in_tables, tables_read = scan_tables(
        store, value, credential_table=credential_table
    )
    return SecretFindings(
        control_found=raw_store_holds(store, value),
        in_responses=tuple(sorted({label for label, body in bodies if value in body})),
        in_logs=in_logs,
        in_tables=in_tables,
        responses_read=len(bodies),
        files_read=files_read,
        tables_read=tables_read,
    )


def credential_table() -> tuple[Precondition, str | None]:
    """The one store table that holds a key by design, named by the installed omnimarket."""
    name = "the local credential store names its table"
    try:
        from omnimarket.inference.local_byok_credential_adapter import (
            LOCAL_CREDENTIAL_TABLE,
        )
    except ImportError as exc:
        return Precondition(
            name,
            False,
            f"omnimarket's local credential store is not importable: {exc}",
            "OMN-19985",
        ), None
    return Precondition(
        name, True, f"table {LOCAL_CREDENTIAL_TABLE}", "OMN-19985"
    ), LOCAL_CREDENTIAL_TABLE


def _run_input(
    argv: list[str], stdin: str, *, env: dict[str, str], timeout: int
) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv)}: timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def plant_credential(
    env: dict[str, str],
    scratch: Path,
    value: str,
    *,
    run_input: Callable[..., tuple[int, str, str]] = _run_input,
) -> Precondition:
    """Set a provider key the way the quickstart does: `onex secret set`, value on stdin.

    Every proxy variable points at a closed local port, so the provider model
    lookup the command makes fails here and the value never leaves the machine;
    the command then stores the key with no model, as it does offline. Its output
    is kept under the scratch tree, where the search reads it too.
    """
    dead = _free_port()
    penv = {k: v for k, v in env.items() if k.lower() != "no_proxy"}
    loopback = str(ipaddress.IPv4Address(0x7F000001))
    dead_proxy = f"http://{loopback}:{dead}"
    penv.update(dict.fromkeys(_PROXY_VARS, dead_proxy))
    rc, out, err = run_input(
        ["onex", "secret", "set", PLANT_REF, "--model", PLANT_MODEL],
        value,
        env=penv,
        timeout=120,
    )
    log = scratch / "logs" / "secret-set.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"{out}\n{err}", encoding="utf-8")
    name = f"a provider key planted with `onex secret set {PLANT_REF} --model {PLANT_MODEL}`"
    if rc != 0:
        said = (err or out).replace(value, "<planted value>").strip()[:200]
        return Precondition(name, False, f"exited {rc}: {said}", "OMN-19985")
    registered = "Registered it as your" in out
    return Precondition(
        name,
        True,
        (
            f"exited 0; value on stdin, never argv; network pointed at a closed local port; "
            f"registered as a route key: {'yes' if registered else 'no'}"
        ),
        "OMN-19985",
    )


def identity_and_fixture(env: dict[str, str], store: Path) -> list[Precondition]:
    """The scratch install's identity and the one fixture run case 1 must see served."""
    pre, tenant = local_tenant_identity(env)
    out = [pre]
    if tenant is not None:
        out.append(seed_fixture_delegation(store, tenant_id=tenant))
    return out


def non_loopback_address() -> str | None:
    """This machine's address on its default route, or None. No packet is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if ipaddress.ip_address(address).is_loopback else address


def read_pages(
    base_url: str, current: Credential, request: Request
) -> list[tuple[str, str]]:
    """The page and every exposure the catalogue lists, as the developer's browser reads them."""
    catalogue = request(current.url(base_url + CATALOGUE_PATH), current.headers())
    out = [
        (
            "page / (current token)",
            request(current.url(base_url + "/"), current.headers()).body,
        ),
        ("page / (no token)", request(base_url + "/", {}).body),
        ("catalogue (current token)", catalogue.body),
    ]
    for topic in sorted(set(_TOPIC.findall(catalogue.body))):
        path = f"/projection/{urllib.parse.quote(topic, safe='')}"
        out.append(
            (
                f"exposure {topic}",
                request(current.url(base_url + path), current.headers()).body,
            )
        )
    return out


def _any_answer(status: int | None) -> bool:
    """A token-guarded server refuses the untokened readiness read; any answer is up."""
    return status is not None


def exercise_auth_cases(
    env: dict[str, str],
    scratch: Path,
    value: str,
    *,
    start: Callable[..., AbstractContextManager[DashboardStart]] = started_dashboard,
    request: Request = http_request,
    lan_address: str | None = None,
    discover: Callable[[], str | None] = non_loopback_address,
) -> tuple[list[Precondition], list[Clause]]:
    """Seed, plant, start the dashboard twice on one port, grade the cases and the search."""
    store = Path(env["HOME"]) / ".omninode" / "delegation" / "delegation.sqlite"
    pres = identity_and_fixture(env, store)
    table_pre, table = credential_table()
    pres.append(table_pre)
    if any(not p.present for p in pres) or table is None:
        return pres, []
    planted = plant_credential(env, scratch, value)
    pres.append(planted)
    if not planted.present:
        return pres, []

    port = _free_port()
    overlay = scratch / "c32-bind.yaml"
    overlay.write_text(f'dashboard:\n  bind: "127.0.0.1:{port}"\n', encoding="utf-8")
    argv = ("onex", "dashboard", "--overlay", str(overlay))
    logs = scratch / "logs"

    first: DashboardStart
    with start(
        env, argv, log_path=logs / "dashboard-1.log", ready=_any_answer
    ) as first:
        stale = (
            credential_from_start(first.printed_url, request)
            if first.base_url
            else Credential()
        )
    pres.append(
        Precondition(
            "onex dashboard starts on loopback (first start)",
            first.base_url is not None,
            first.detail,
            "OMN-19976",
        )
    )
    if first.base_url is None:
        return pres, []

    lan = lan_address if lan_address is not None else discover()
    with start(
        env, argv, log_path=logs / "dashboard-2.log", ready=_any_answer
    ) as second:
        if second.base_url is not None:
            current = credential_from_start(second.printed_url, request)
            obs = observe(
                second.base_url,
                current,
                stale,
                request=request,
                lan_address=lan,
                correlation_id=FIXTURE_CORRELATION_ID,
            )
            pages = read_pages(second.base_url, current, request)
    pres.append(
        Precondition(
            "onex dashboard starts on loopback (second start)",
            second.base_url is not None,
            second.detail,
            "OMN-19976",
        )
    )
    if second.base_url is None:
        return pres, []

    clauses = grade_auth_cases(obs, correlation_id=FIXTURE_CORRELATION_ID)
    clauses.append(
        grade_planted_secret(
            secret_findings(
                value=value,
                scratch=scratch,
                store=store,
                credential_table=table,
                bodies=obs.bodies() + pages,
            )
        )
    )
    return pres, clauses


# ------------------------------------------------- the start-time LAN bind


LAN_BIND_HOST = "0.0.0.0"
LAN_BIND_CLAUSE = (
    "a non-loopback dashboard.bind is refused at start and nothing listens"
)
_URL = re.compile(r"https?://\S+:\d+")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def grade_lan_bind(
    *,
    control_started: bool,
    control_detail: str,
    lan_rc: int,
    lan_printed_url: bool,
    lan_listening: bool,
) -> Clause:
    """Refused means: the loopback control came up, the LAN bind exited non-zero
    without printing a URL (124 is our timeout, i.e. it kept serving), and
    nothing answered on its port afterwards."""
    refused = lan_rc not in (0, 124) and not lan_printed_url and not lan_listening
    if not control_started:
        return Clause(
            LAN_BIND_CLAUSE,
            False,
            f"positive control failed, so a refusal proves nothing: {control_detail}",
        )
    return Clause(
        LAN_BIND_CLAUSE,
        refused,
        f"loopback control: {control_detail}; {LAN_BIND_HOST} bind exited {lan_rc}; "
        f"printed a URL={lan_printed_url}; port listening afterwards={lan_listening}",
    )


def lan_bind_check(
    env: dict[str, str],
    scratch: Path,
    *,
    run: Callable[..., tuple[int, str, str]] = _run,
    start: Callable[..., AbstractContextManager[tuple[str | None, str]]] = (
        running_dashboard
    ),
    listening: Callable[[int], bool] = _listening,
) -> Clause:
    """Run the loopback control and the LAN bind against the real `onex dashboard`."""
    loop_port, lan_port = _free_port(), _free_port()
    loop_overlay, lan_overlay = scratch / "loopback.yaml", scratch / "lan.yaml"
    loop_overlay.write_text(
        f'dashboard:\n  bind: "127.0.0.1:{loop_port}"\n', encoding="utf-8"
    )
    lan_overlay.write_text(
        f'dashboard:\n  bind: "{LAN_BIND_HOST}:{lan_port}"\n', encoding="utf-8"
    )
    with start(env, ("onex", "dashboard", "--overlay", str(loop_overlay))) as (
        base_url,
        detail,
    ):
        control_started = base_url is not None
    rc, out, _err = run(
        ["onex", "dashboard", "--overlay", str(lan_overlay)], timeout=30, env=env
    )
    return grade_lan_bind(
        control_started=control_started,
        control_detail=detail,
        lan_rc=rc,
        lan_printed_url=_URL.search(out or "") is not None,
        lan_listening=listening(lan_port),
    )


def run(
    *,
    lan_check: Callable[[dict[str, str], Path], Clause] | None = None,
    auth_check: (
        Callable[[dict[str, str], Path, str], tuple[list[Precondition], list[Clause]]]
        | None
    ) = None,
) -> ProbeResult:
    """Grade C32. `lan_check` and `auth_check` are injectable so the unit tests start nothing."""
    lan_check = lan_check if lan_check is not None else lan_bind_check
    auth_check = auth_check if auth_check is not None else exercise_auth_cases
    result = ProbeResult(criterion=CRITERION)
    result.preconditions.append(onex_cli())
    dashboard_command = onex_subcommand("dashboard", owner="OMN-19976")
    secret_command = onex_subcommand("secret", owner="OMN-19985")
    result.preconditions.extend([dashboard_command, secret_command])
    if not (dashboard_command.present and secret_command.present):
        return result
    # Generated per run and never printed: only where it was found is recorded.
    value = PLANT_PREFIX + secrets.token_hex(24)
    with tempfile.TemporaryDirectory(
        prefix="c32-", dir=os.environ.get("RUNNER_TEMP") or None
    ) as tmp:
        scratch = Path(tmp) / "auth"
        (scratch / "home").mkdir(parents=True)
        pres, clauses = auth_check(
            dict(os.environ, HOME=str(scratch / "home")), scratch, value
        )
        result.preconditions.extend(pres)
        result.clauses.extend(clauses)
        lan_scratch = Path(tmp) / "lan"
        (lan_scratch / "home").mkdir(parents=True)
        result.clauses.append(
            lan_check(dict(os.environ, HOME=str(lan_scratch / "home")), lan_scratch)
        )
    return result
