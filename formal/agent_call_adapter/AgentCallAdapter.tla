------------------------------ MODULE AgentCallAdapter ------------------------------
(* OMN-21096, plan task TB.1: the Agent-call adapter and the client-call claim.  *)
(*                                                                               *)
(* One model-raised foreground Agent call ("orig") is intercepted by the         *)
(* binding, which runs the adapter. The adapter resolves its max mode from the   *)
(* client section, then journals (observe) or publishes (selective, required)    *)
(* the delegate-skill command and maps what it observes onto proceed_native,     *)
(* answer or refuse through the verdict table. The orchestrator owns one         *)
(* client-call claim row per client_call_key and moves it through claimed,       *)
(* admitted / not_admitted / observed, dispatch_intended and terminal, with      *)
(* every transition a guarded write on the row's generation.                     *)
(*                                                                               *)
(* Faults: an orchestrator crash between every pair of steps (claim, admission   *)
(* commit, admission publish, dispatch-intent commit, dispatch publish, terminal *)
(* store, terminal publish), lease expiry and takeover, a stale owner writing    *)
(* after takeover, broker and drainer redelivery, a binding reload re-running    *)
(* the adapter, a conflicting command ("conf") with the same key and a          *)
(* different fingerprint, a server mode change while commands are in flight,     *)
(* and client and server disagreeing on mode (the client max, the server mode,  *)
(* the allowlist and the sensitivity verdict are chosen freely in Init).        *)
(*                                                                               *)
(* Each design knob below is TRUE/FALSE in the design config; each mutant flips  *)
(* exactly one and must violate the property named in README.txt.               *)
EXTENDS Naturals, FiniteSets

CONSTANTS
    MaxCrash, MaxExpire, MaxRedeliver, MaxRerun, MaxModeChange, MaxGen,
    \* design knobs (design value in brackets)
    FenceByGeneration,      \* [TRUE]  guarded writes WHERE generation = g
    IntentBeforeDispatch,   \* [TRUE]  dispatch intent commits before delegation-request publish
    ClaimInsertIfAbsent,    \* [TRUE]  INSERT ... ON CONFLICT DO NOTHING; a live row is never reset
    ReconcileNeverRedispatch, \* [TRUE] a dispatch_intended row is never dispatched again
    CorrFromFingerprint,    \* [TRUE]  correlation_id = uuid5(key + fingerprint)
    EchoAfterCommit,        \* [TRUE]  admission event published after the admission commit
    EffectiveIsMin,         \* [TRUE]  effective = min(client max, server mode)
    NativeOnlyOnConfirm,    \* [TRUE]  admission unknown refuses, never proceeds native
    BindingFailClosed,      \* [TRUE]  adapter never started on a required host maps to deny
    AnswerNeedsAccepted     \* [TRUE]  answer only on completed, gate-passed, identity-matched terminal

Modes == {"observe", "selective", "required"}
Rank(m) == CASE m = "observe" -> 0 [] m = "selective" -> 1 [] m = "required" -> 2
Min(a, b) == IF Rank(a) <= Rank(b) THEN a ELSE b
Cmds == {"orig", "conf"}
Fp(c) == IF c = "orig" THEN 1 ELSE 2
\* both commands share client_call_key; the correlation id folds in the fingerprint
Corr(c) == IF CorrFromFingerprint THEN Fp(c) ELSE 0
None == [state |-> "none", fp |-> 0, gen |-> 0, lease |-> FALSE, eff |-> "none",
         adm |-> FALSE, term |-> "none"]

VARIABLES
    clientMax, allowlisted, sensitive,   \* fixed per behaviour, chosen in Init
    serverMode, modeChanges,
    ad, adSeen, reruns,                  \* adapter (client side)
    published, inflight, redeliveries,   \* bus: command deliveries pending per command
    row,                                 \* the client-call claim row for the shared key
    handlers, nextId, crashes, expiries, \* orchestrator handler incarnations
    evts,                                \* admission and terminal events on the bus
    dispatchLog, chainRes                \* delegation-request publishes and the chain's terminal

vars == <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen, reruns,
          published, inflight, redeliveries, row, handlers, nextId, crashes, expiries,
          evts, dispatchLog, chainRes>>

AdStates == {"idle", "received", "published", "awaiting", "answered", "refused", "native"}
Pcs == {"claim", "admit", "commitAdm", "pubAdm", "intent", "commitIntent", "dispatch",
        "await", "compose", "pubTerm"}

TypeOK ==
    /\ clientMax \in Modes /\ serverMode \in Modes
    /\ ad \in AdStates
    /\ row.state \in {"none", "claimed", "admitted", "not_admitted", "observed",
                      "dispatch_intended", "terminal"}
    /\ \A h \in handlers : h.pc \in Pcs /\ h.c \in Cmds
    /\ inflight \in [Cmds -> Nat]

Init ==
    /\ clientMax \in Modes /\ serverMode \in Modes
    /\ allowlisted \in BOOLEAN /\ sensitive \in BOOLEAN
    /\ modeChanges = 0
    /\ ad = "idle" /\ adSeen = {} /\ reruns = 0
    /\ published = [c \in Cmds |-> FALSE] /\ inflight = [c \in Cmds |-> 0]
    /\ redeliveries = 0
    /\ row = None
    /\ handlers = {} /\ nextId = 1 /\ crashes = 0 /\ expiries = 0
    /\ evts = {}
    /\ dispatchLog = {} /\ chainRes = "none"

Publish(c) ==
    /\ published' = [published EXCEPT ![c] = TRUE]
    /\ inflight' = [inflight EXCEPT ![c] = @ + 1]

AdmEvt(c, eff, adm) == [kind |-> "adm", src |-> c, corr |-> Corr(c), fp |-> Fp(c),
                        eff |-> eff, adm |-> adm, status |-> "none"]
TermEvt(c, st) == [kind |-> "term", src |-> c, corr |-> Corr(c), fp |-> Fp(c),
                   eff |-> "none", adm |-> FALSE, status |-> st]

-----------------------------------------------------------------------------
(* Binding and adapter *)

\* The binding runs the adapter, or the adapter never starts. The host's max mode is
\* rendered into the binding's failure table, so a required host denies.
BindStart ==
    /\ ad = "idle"
    /\ \/ ad' = "received"
       \/ ad' = IF clientMax = "required" /\ BindingFailClosed THEN "refused" ELSE "native"
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, adSeen, reruns,
                   published, inflight, redeliveries, row, handlers, nextId, crashes,
                   expiries, evts, dispatchLog, chainRes>>

AdResolve ==
    /\ ad = "received"
    /\ \/ /\ clientMax = "observe"            \* journal; the drainer publishes; never wait
          /\ Publish("orig") /\ ad' = "native"
       \/ /\ clientMax # "observe"            \* refused before publish: nothing published
          /\ ad' = IF clientMax = "selective" THEN "native" ELSE "refused"
          /\ UNCHANGED <<published, inflight>>
       \/ /\ clientMax # "observe"
          /\ Publish("orig") /\ ad' = "published"
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, adSeen, reruns,
                   redeliveries, row, handlers, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

Mine(e) == e.corr = Corr("orig")

AdAdmission ==
    /\ ad = "published"
    /\ \E e \in evts :
        /\ e.kind = "adm" /\ Mine(e)
        /\ adSeen' = adSeen \cup {e}
        /\ ad' = IF e.eff = "observe" THEN "native"
                 ELSE IF e.adm THEN "awaiting"
                 ELSE IF e.eff = "required" /\ allowlisted THEN "refused"
                 ELSE "native"
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, reruns,
                   published, inflight, redeliveries, row, handlers, nextId, crashes,
                   expiries, evts, dispatchLog, chainRes>>

Accepted(e) == e.status = "ok" /\ e.fp = Fp("orig")

AdTerminal ==
    /\ ad \in {"published", "awaiting"}
    /\ \E e \in evts :
        /\ e.kind = "term" /\ Mine(e)
        /\ adSeen' = adSeen \cup {e}
        /\ ad' = IF ~AnswerNeedsAccepted \/ (ad = "awaiting" /\ Accepted(e))
                 THEN "answered" ELSE "refused"
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, reruns,
                   published, inflight, redeliveries, row, handlers, nextId, crashes,
                   expiries, evts, dispatchLog, chainRes>>

\* Deadline A (admission unknown) or the terminal deadline passes.
AdTimeout ==
    /\ ad \in {"published", "awaiting"}
    /\ ad' = IF ad = "published" /\ ~NativeOnlyOnConfirm /\ clientMax = "selective"
             THEN "native" ELSE "refused"
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, adSeen, reruns,
                   published, inflight, redeliveries, row, handlers, nextId, crashes,
                   expiries, evts, dispatchLog, chainRes>>

\* A binding reload re-runs the adapter for the same tool call: same key and fingerprint,
\* so it republishes the same command and reattaches by correlation id.
AdRerun ==
    /\ ad \in {"published", "awaiting"} /\ reruns < MaxRerun
    /\ reruns' = reruns + 1 /\ Publish("orig") /\ ad' = "published"
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, adSeen,
                   redeliveries, row, handlers, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

\* Another adapter publishes a command with the same client_call_key and another request.
PublishConflict ==
    /\ ~published["conf"] /\ Publish("conf")
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, redeliveries, row, handlers, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

-----------------------------------------------------------------------------
(* Bus, server mode, chain *)

Redeliver ==
    /\ redeliveries < MaxRedeliver
    /\ \E c \in Cmds : published[c] /\ inflight' = [inflight EXCEPT ![c] = @ + 1]
    /\ redeliveries' = redeliveries + 1
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, row, handlers, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

ModeChange ==
    /\ modeChanges < MaxModeChange
    /\ serverMode' \in Modes \ {serverMode} /\ modeChanges' = modeChanges + 1
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, ad, adSeen, reruns, published,
                   inflight, redeliveries, row, handlers, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

ChainFinish ==
    /\ dispatchLog # {} /\ chainRes = "none"
    /\ chainRes' \in {"ok", "fail"}
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, row, handlers, nextId, crashes,
                   expiries, evts, dispatchLog>>

LeaseExpire ==
    /\ expiries < MaxExpire
    /\ row.state \notin {"none", "terminal"} /\ row.lease
    /\ row' = [row EXCEPT !.lease = FALSE] /\ expiries' = expiries + 1
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, handlers, nextId, crashes,
                   evts, dispatchLog, chainRes>>

-----------------------------------------------------------------------------
(* Orchestrator handlers *)

H(h, f) == (handlers \ {h}) \cup {f}
Stop(h) == handlers \ {h}
\* A guarded write affects the row only when the handler still holds its generation.
CanWrite(h) == ~FenceByGeneration \/ row.gen = h.gen

\* Where a takeover resumes, from the row's recorded state.
ResumePc(s) ==
    IF s = "claimed" THEN "admit"
    ELSE IF s = "dispatch_intended"
         THEN IF ReconcileNeverRedispatch THEN "await" ELSE "dispatch"
    ELSE "pubAdm"

Takeover(c) ==
    [id |-> nextId, c |-> c, gen |-> row.gen + 1, pc |-> ResumePc(row.state),
     acked |-> TRUE, eff |-> row.eff, adm |-> row.adm, term |-> row.term]

Consume ==
    \E c \in Cmds :
        /\ inflight[c] > 0
        /\ inflight' = [inflight EXCEPT ![c] = @ - 1]
        /\ handlers' = handlers \cup {[id |-> nextId, c |-> c, gen |-> 0, pc |-> "claim",
                                       acked |-> FALSE, eff |-> "none", adm |-> FALSE,
                                       term |-> "none"]}
        /\ nextId' = nextId + 1
        /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                       reruns, published, redeliveries, row, crashes, expiries, evts,
                       dispatchLog, chainRes>>

\* The claim commits before the consumer commits the offset (acked).
Claim(h) ==
    /\ h.pc = "claim"
    /\ IF row.state = "none" THEN
              /\ row' = [None EXCEPT !.state = "claimed", !.fp = Fp(h.c), !.gen = 1, !.lease = TRUE]
              /\ handlers' = H(h, [h EXCEPT !.gen = 1, !.pc = "admit", !.acked = TRUE])
              /\ UNCHANGED <<evts, nextId>>
       ELSE IF row.fp # Fp(h.c) THEN          \* conflict: own correlation, no write
              /\ evts' = evts \cup {TermEvt(h.c, "client_call_conflict")}
              /\ handlers' = Stop(h) /\ UNCHANGED <<row, nextId>>
       ELSE IF row.state = "terminal" THEN    \* republish the stored terminal
              /\ evts' = evts \cup {TermEvt(h.c, row.term)}
              /\ handlers' = Stop(h) /\ UNCHANGED <<row, nextId>>
       ELSE IF ~ClaimInsertIfAbsent /\ row.gen < MaxGen THEN  \* blind upsert resets the row
              /\ row' = [None EXCEPT !.state = "claimed", !.fp = Fp(h.c), !.gen = row.gen + 1,
                                     !.lease = TRUE]
              /\ handlers' = H(h, [h EXCEPT !.gen = row.gen + 1, !.pc = "admit", !.acked = TRUE])
              /\ UNCHANGED <<evts, nextId>>
       ELSE IF row.lease \/ row.gen >= MaxGen THEN  \* live owner: await the shared correlation
              /\ handlers' = Stop(h) /\ UNCHANGED <<row, evts, nextId>>
       ELSE                                    \* lease expired: take over
              /\ row' = [row EXCEPT !.gen = @ + 1, !.lease = TRUE]
              /\ handlers' = H(h, [Takeover(h.c) EXCEPT !.id = h.id])
              /\ UNCHANGED <<evts, nextId>>
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, crashes, expiries,
                   dispatchLog, chainRes>>

\* The reconciler takes over a non-terminal row whose lease expired, with no redelivery.
Reconcile ==
    /\ row.state \notin {"none", "terminal"} /\ ~row.lease /\ row.gen < MaxGen
    /\ row' = [row EXCEPT !.gen = @ + 1, !.lease = TRUE]
    /\ handlers' = handlers \cup {Takeover(IF row.fp = 1 THEN "orig" ELSE "conf")}
    /\ nextId' = nextId + 1
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, crashes, expiries, evts,
                   dispatchLog, chainRes>>

Decide(h) ==
    LET eff == IF EffectiveIsMin THEN Min(clientMax, serverMode) ELSE serverMode
        adm == allowlisted /\ ~sensitive /\ eff # "observe"
    IN [h EXCEPT !.eff = eff, !.adm = adm]

AdmState(h) == IF h.eff = "observe" THEN "observed" ELSE IF h.adm THEN "admitted" ELSE "not_admitted"

Admit(h) ==
    /\ h.pc = "admit"
    /\ LET d == Decide(h) IN
       IF EchoAfterCommit
       THEN IF CanWrite(h)
            THEN /\ row' = [row EXCEPT !.state = AdmState(d), !.eff = d.eff, !.adm = d.adm,
                                       !.lease = TRUE]
                 /\ handlers' = H(h, [d EXCEPT !.pc = "pubAdm"]) /\ UNCHANGED evts
            ELSE /\ handlers' = Stop(h) /\ UNCHANGED <<row, evts>>
       ELSE /\ evts' = evts \cup {AdmEvt(h.c, d.eff, d.adm)}
            /\ handlers' = H(h, [d EXCEPT !.pc = "commitAdm"]) /\ UNCHANGED row
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, nextId, crashes, expiries,
                   dispatchLog, chainRes>>

\* Mutant path only: the admission commit after its event was already published.
CommitAdm(h) ==
    /\ h.pc = "commitAdm"
    /\ IF CanWrite(h)
       THEN /\ row' = [row EXCEPT !.state = AdmState(h), !.eff = h.eff, !.adm = h.adm,
                                  !.lease = TRUE]
            /\ handlers' = H(h, [h EXCEPT !.pc = IF h.adm THEN "intent" ELSE "compose"])
       ELSE /\ handlers' = Stop(h) /\ UNCHANGED row
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

PubAdm(h) ==
    /\ h.pc = "pubAdm"
    /\ evts' = evts \cup {AdmEvt(h.c, h.eff, h.adm)}
    /\ handlers' = H(h, [h EXCEPT !.pc = IF h.adm THEN "intent" ELSE "compose"])
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, row, nextId, crashes, expiries,
                   dispatchLog, chainRes>>

DoDispatch(h) == dispatchLog' = dispatchLog \cup {[id |-> h.id, gen |-> h.gen, c |-> h.c, eff |-> h.eff]}

Intent(h) ==
    /\ h.pc = "intent"
    /\ IF IntentBeforeDispatch
       THEN IF CanWrite(h)
            THEN /\ row' = [row EXCEPT !.state = "dispatch_intended", !.lease = TRUE]
                 /\ handlers' = H(h, [h EXCEPT !.pc = "dispatch"]) /\ UNCHANGED dispatchLog
            ELSE /\ handlers' = Stop(h) /\ UNCHANGED <<row, dispatchLog>>
       ELSE /\ DoDispatch(h)                     \* mutant: publish, then commit intent
            /\ handlers' = H(h, [h EXCEPT !.pc = "commitIntent"]) /\ UNCHANGED row
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, nextId, crashes, expiries, evts,
                   chainRes>>

CommitIntent(h) ==
    /\ h.pc = "commitIntent"
    /\ IF CanWrite(h)
       THEN /\ row' = [row EXCEPT !.state = "dispatch_intended", !.lease = TRUE]
            /\ handlers' = H(h, [h EXCEPT !.pc = "await"])
       ELSE /\ handlers' = Stop(h) /\ UNCHANGED row
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

Dispatch(h) ==
    /\ h.pc = "dispatch"
    /\ DoDispatch(h)
    /\ handlers' = H(h, [h EXCEPT !.pc = "await"])
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, row, nextId, crashes, expiries,
                   evts, chainRes>>

StoreTerminal(h, st) ==
    IF CanWrite(h)
    THEN /\ row' = [row EXCEPT !.state = "terminal", !.term = st, !.lease = TRUE]
         /\ handlers' = H(h, [h EXCEPT !.pc = "pubTerm", !.term = st])
    ELSE /\ handlers' = Stop(h) /\ UNCHANGED row

\* The chain's terminal for the dispatch, or reconciliation past the deadline.
Await(h) ==
    /\ h.pc = "await"
    /\ \/ chainRes # "none" /\ StoreTerminal(h, chainRes)
       \/ chainRes = "none" /\ StoreTerminal(h, "dispatch_outcome_unknown")
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

Compose(h) ==
    /\ h.pc = "compose"
    /\ StoreTerminal(h, IF h.eff = "observe" THEN "observed" ELSE "not_admitted")
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, nextId, crashes, expiries, evts,
                   dispatchLog, chainRes>>

PubTerm(h) ==
    /\ h.pc = "pubTerm"
    /\ evts' = evts \cup {TermEvt(h.c, h.term)}
    /\ handlers' = Stop(h)
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, inflight, redeliveries, row, nextId, crashes, expiries,
                   dispatchLog, chainRes>>

\* A crash between any two steps; an un-acked delivery is redelivered by the broker.
Crash(h) ==
    /\ crashes < MaxCrash
    /\ crashes' = crashes + 1
    /\ handlers' = Stop(h)
    /\ inflight' = IF h.acked THEN inflight ELSE [inflight EXCEPT ![h.c] = @ + 1]
    /\ UNCHANGED <<clientMax, allowlisted, sensitive, serverMode, modeChanges, ad, adSeen,
                   reruns, published, redeliveries, row, nextId, expiries, evts,
                   dispatchLog, chainRes>>

HandlerStep ==
    \E h \in handlers :
        \/ Claim(h) \/ Admit(h) \/ CommitAdm(h) \/ PubAdm(h) \/ Intent(h)
        \/ CommitIntent(h) \/ Dispatch(h) \/ Await(h) \/ Compose(h) \/ PubTerm(h) \/ Crash(h)

Next ==
    \/ BindStart \/ AdResolve \/ AdAdmission \/ AdTerminal \/ AdTimeout \/ AdRerun
    \/ PublishConflict \/ Redeliver \/ ModeChange \/ ChainFinish \/ LeaseExpire
    \/ Consume \/ Reconcile \/ HandlerStep

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* Properties (plan section 7.2, "Invariants (TB.1)") *)

OrigDispatched == \E d \in dispatchLog : d.c = "orig"

\* Native runs only when nothing was dispatched for the call, ever: nothing published,
\* effective observe, or a committed admitted=false.
NativeOnlyWhenConfirmedNotDispatched == ad = "native" => ~OrigDispatched

\* On a required host an allowlisted call runs natively only when the server echoed a
\* lowered effective mode for it.
RequiredAllowlistedNeverNative ==
    (ad = "native" /\ allowlisted /\ clientMax = "required")
        => \E e \in adSeen : e.kind = "adm" /\ e.eff # "required"

AtMostOneDispatchPerClientCall == Cardinality(dispatchLog) <= 1

\* No event of the conflicting command is ever taken by the original adapter.
ConflictNeverReachesOriginal == \A e \in adSeen : e.src = "orig"

ObserveNeverDispatches ==
    \A d \in dispatchLog : d.eff # "observe" /\ clientMax # "observe"

AnswerOnlyOnAcceptedResult ==
    ad = "answered" =>
        /\ OrigDispatched
        /\ \E e \in adSeen : e.kind = "term" /\ e.src = "orig" /\ Accepted(e)
=============================================================================
