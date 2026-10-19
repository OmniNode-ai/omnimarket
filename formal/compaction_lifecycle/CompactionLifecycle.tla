------------------------- MODULE CompactionLifecycle -------------------------
(***************************************************************************)
(* The compaction lifecycle of the context service (OMN-21104, plan task   *)
(* TA.1), modelled before the compaction orchestrator (TA.11) and the      *)
(* install-confirm compute (TA.13) are built.                              *)
(*                                                                         *)
(* One session. Its transcript is a sequence of records, each a turn or a  *)
(* compaction boundary; the candidate's inputs are the transcript as it    *)
(* stood when the invocation started, so a candidate is named by that      *)
(* length (the version vector) and two invocations on the same inputs      *)
(* build the same candidate. An invocation is started by the precompute,   *)
(* manual or auto trigger; manual and auto hold the turn, precompute does  *)
(* not. The orchestrator builds (publishing prepared), mints the           *)
(* activation (publishing activated) and sends the package; any step may  *)
(* fail before the send, which publishes fallback and replies native. The *)
(* edge has its own deadline and may miss it while the orchestrator still *)
(* completes. The harness writes the boundary: the activation the edge    *)
(* installed, a kept precompute, or native compaction. Capture journals   *)
(* each transcript record late, in any order, or drops it, and may record *)
(* a truncated boundary. The install-confirm compute derives installation *)
(* facts from the captured records alone; receipt events reach the fold   *)
(* in any order and any number of times.                                   *)
(*                                                                         *)
(* An activation is named by the invocation that minted it (at most one   *)
(* per invocation). Each design knob is one decision of the design note;  *)
(* every mutant config flips exactly one and must violate the property it *)
(* protects.                                                               *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    Inv,            \* invocation ids (an activation is named by its invocation)
    Triggers,       \* subset of {"precompute", "manual", "auto"}
    MaxLen,         \* transcript length bound (exploration bound)
    MaxDrops,       \* capture drops in one behaviour (exploration bound)
    ActivateFirst,  \* activated is published before the package is sent
    RevalidateOn,   \* a kept precompute is used only if its inputs are current
    FoldMode,       \* "join": the fold is a join of the events received; "overwrite": last arrival wins
    FallbackKey,    \* "invocation": fallback folds by invocation_id; "candidate": by candidate_id
    CompleteOn,     \* "installed": complete only on an installed fact; "sent": when the package is sent
    GapRule,        \* a later captured item with no boundary after the activation resolves it to uncertain(capture_gap)
    BarrierOn,      \* the build waits for gap-free, untruncated sources up to the candidate's inputs
    MaxSv,          \* source-version bound: ledger and checkpoint changes (exploration bound)
    AsmHashes       \* assembly hashes a build can produce; two values model a determinism bug

None == 0
NoCand == 99

\* A candidate is named by its version vector: the transcript length it was
\* built on and the version of the other sources (ledger, checkpoint).
CandId(len, v) == len * 10 + v
LenOf(c) == c \div 10
SvOf(c) == c % 10
Cands == {CandId(l, v) : l \in 0..MaxLen, v \in 0..MaxSv}

\* Activation states, ordered for the fold. installed and install_mismatch are
\* incomparable and join to conflict; every other pair is ordered, so the join
\* is commutative, associative and idempotent.
AStates == {"none", "activated", "uncertain", "not_selected", "native",
            "installed", "install_mismatch", "conflict"}
Rank(s) ==
    CASE s = "none"             -> 0
      [] s = "activated"        -> 1
      [] s = "uncertain"        -> 2
      [] s = "not_selected"     -> 3
      [] s = "native"           -> 4
      [] s = "installed"        -> 5
      [] s = "install_mismatch" -> 5
      [] s = "conflict"         -> 6
Join(x, y) ==
    IF x = y THEN x
    ELSE IF Rank(x) > Rank(y) THEN x
    ELSE IF Rank(y) > Rank(x) THEN y
    ELSE "conflict"
Resolved == {"uncertain", "not_selected", "native", "installed", "install_mismatch"}

\* Receipt events. One record shape so that TLC compares like with like.
\*   k: prepared | activated | fallback | installed | install_mismatch | native
\*      | not_selected | uncertain
\*   a: the activation (or invocation for fallback); None for prepared
\*   c: the candidate, NoCand when none was built
\*   x: the assembly hash for prepared; for activated, the session high-water
\*      of the request (the barrier it carried); else 0
Ev(k, a, c, x) == [k |-> k, a |-> a, c |-> c, x |-> x]
ActKinds == {"activated", "installed", "install_mismatch", "native",
             "not_selected", "uncertain"}

\* A boundary records the version vector its trigger fixed (tv): the request's
\* inputs for a trigger that built its own candidate, the vector current at
\* revalidation for a kept precompute.
Rec(kind, act, altered, tv) == [kind |-> kind, act |-> act, altered |-> altered, tv |-> tv]

\* Candidate fold states: one assembly hash seen, or conflict.
CStates == {"none", "conflict"} \cup {"h" \o ToString(h) : h \in AsmHashes}
CJoin(x, y) == IF x = y \/ y = "none" THEN x ELSE IF x = "none" THEN y ELSE "conflict"

VARIABLES
    tr,        \* the session transcript
    sv,        \* the version of the other sources (ledger, checkpoint)
    holding,   \* the invocation holding the turn (manual or auto), or None
    trig,      \* trigger per invocation
    ph,        \* orchestrator phase per invocation
    er,        \* edge result per invocation: wait | install | native
    cand,      \* candidate per invocation: the transcript length at its start
    kept,      \* precompute activations whose package the harness holds
    captured,  \* transcript positions captured
    dropped,   \* transcript positions whose capture was lost
    trunc,     \* captured positions whose record is truncated
    bus,       \* receipt events published (durable once acknowledged)
    seen,      \* events the fold has received (the reference set)
    astate,    \* the fold's incremental state per activation
    cstate,    \* the fold's incremental state per candidate
    istate,    \* the fold's incremental state per invocation (fallback)
    complete,  \* activations reported complete
    overGap    \* ghost: invocations that built over incomplete sources

vars == <<tr, sv, holding, trig, ph, er, cand, kept, captured, dropped, trunc,
          bus, seen, astate, cstate, istate, complete, overGap>>

Phases == {"idle", "started", "built", "activated", "sent", "failed", "done"}
Positions == 1..MaxLen

TypeOK ==
    /\ tr \in Seq([kind : {"turn", "boundary"}, act : Inv \cup {None}, altered : BOOLEAN,
                    tv : Cands \cup {NoCand}])
    /\ sv \in 0..MaxSv
    /\ Len(tr) <= MaxLen
    /\ holding \in Inv \cup {None}
    /\ trig \in [Inv -> Triggers \cup {"none"}]
    /\ ph \in [Inv -> Phases]
    /\ er \in [Inv -> {"wait", "install", "native"}]
    /\ cand \in [Inv -> Cands \cup {NoCand}]
    /\ kept \subseteq Inv
    /\ captured \subseteq Positions /\ dropped \subseteq Positions /\ trunc \subseteq captured
    /\ seen \subseteq bus
    /\ astate \in [Inv -> AStates]
    /\ cstate \in [Cands -> CStates]
    /\ istate \in [Inv -> {"none", "fallback"}]
    /\ complete \subseteq Inv
    /\ overGap \subseteq Inv

Init ==
    /\ tr = <<>>
    /\ sv = 0
    /\ holding = None
    /\ trig = [i \in Inv |-> "none"]
    /\ ph = [i \in Inv |-> "idle"]
    /\ er = [i \in Inv |-> "wait"]
    /\ cand = [i \in Inv |-> NoCand]
    /\ kept = {}
    /\ captured = {} /\ dropped = {} /\ trunc = {}
    /\ bus = {} /\ seen = {}
    /\ astate = [i \in Inv |-> "none"]
    /\ cstate = [c \in Cands |-> "none"]
    /\ istate = [i \in Inv |-> "none"]
    /\ complete = {}
    /\ overGap = {}

FoldVars == <<seen, astate, cstate, istate>>
CaptureVars == <<captured, dropped, trunc>>

----------------------------------------------------------------------------
(* The session and the harness *)

\* A turn advances the conversation, so every candidate built before it is stale.
Turn ==
    /\ holding = None
    /\ Len(tr) < MaxLen
    /\ tr' = Append(tr, Rec("turn", None, FALSE, NoCand))
    /\ UNCHANGED <<sv, overGap, holding, trig, ph, er, cand, kept, CaptureVars, bus, FoldVars, complete>>

\* A ledger row or checkpoint lands: the version vector changes without a turn.
\* It is not held by a trigger holding the turn.
SourceChange ==
    /\ sv < MaxSv
    /\ sv' = sv + 1
    /\ UNCHANGED <<overGap, tr, holding, trig, ph, er, cand, kept, CaptureVars, bus, FoldVars, complete>>

Start(i, t) ==
    /\ ph[i] = "idle"
    /\ Len(tr) < MaxLen
    /\ t # "precompute" => holding = None
    /\ ph' = [ph EXCEPT ![i] = "started"]
    /\ trig' = [trig EXCEPT ![i] = t]
    /\ cand' = [cand EXCEPT ![i] = CandId(Len(tr), sv)]
    /\ holding' = IF t = "precompute" THEN holding ELSE i
    /\ UNCHANGED <<sv, overGap, tr, er, kept, CaptureVars, bus, FoldVars, complete>>

\* The harness writes the boundary for the trigger holding the turn: the
\* activation its edge installed, or native compaction. The harness may write a
\* list other than the one returned (altered), which install-confirm must see.
\* A boundary changes the inputs, so the harness drops every kept precompute.
Compact(i) ==
    /\ holding = i
    /\ er[i] \in {"install", "native"}
    /\ \E alt \in BOOLEAN :
        /\ (er[i] = "native" => ~alt)
        /\ tr' = IF er[i] = "install"
                 THEN Append(tr, Rec("boundary", i, alt, CandId(LenOf(cand[i]), SvOf(cand[i]))))
                 ELSE Append(tr, Rec("boundary", None, FALSE, NoCand))
    /\ holding' = None
    /\ kept' = {}
    /\ UNCHANGED <<sv, overGap, trig, ph, er, cand, CaptureVars, bus, FoldVars, complete>>

\* The holding trigger uses a kept precompute instead of building its own
\* (probe P7a). Revalidation (P7c) refuses a precompute whose inputs changed.
UseKept(i, k) ==
    /\ holding = i
    /\ ph[i] = "started"
    /\ k \in kept
    /\ RevalidateOn => cand[k] = CandId(Len(tr), sv)
    /\ \E alt \in BOOLEAN : tr' = Append(tr, Rec("boundary", k, alt, CandId(Len(tr), sv)))
    /\ ph' = [ph EXCEPT ![i] = "done"]
    /\ holding' = None
    /\ kept' = {}
    /\ UNCHANGED <<sv, overGap, trig, er, cand, CaptureVars, bus, FoldVars, complete>>

----------------------------------------------------------------------------
(* The orchestrator *)

\* The sources are complete for a candidate when every transcript record of its
\* inputs is captured gap-free and untruncated (the session barrier, TA.21).
SourcesComplete(c) == 1..LenOf(c) \subseteq captured \ trunc

\* With the barrier the build waits for complete sources; a source that can
\* never complete (a dropped record) ends in Fail (SOURCE_INCOMPLETE). A build
\* may produce any hash in AsmHashes: a determinism bug the fold must alarm.
Build(i) ==
    /\ ph[i] = "started"
    /\ BarrierOn => SourcesComplete(cand[i])
    /\ ph' = [ph EXCEPT ![i] = "built"]
    /\ \E h \in AsmHashes : bus' = bus \cup {Ev("prepared", None, cand[i], h)}
    /\ overGap' = IF SourcesComplete(cand[i]) THEN overGap ELSE overGap \cup {i}
    /\ UNCHANGED <<sv, tr, holding, trig, er, cand, kept, CaptureVars, FoldVars, complete>>

\* Any failure before the send publishes fallback, carrying the candidate if
\* one was built, and replies native.
Fail(i) ==
    /\ ph[i] \in {"started", "built"}
    /\ ph' = [ph EXCEPT ![i] = "failed"]
    /\ bus' = bus \cup {Ev("fallback", i, IF ph[i] = "built" THEN cand[i] ELSE NoCand, 0)}
    /\ er' = IF er[i] = "wait" THEN [er EXCEPT ![i] = "native"] ELSE er
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, cand, kept, CaptureVars, FoldVars, complete>>

Activate(i) ==
    /\ ph[i] = "built"
    /\ ph' = [ph EXCEPT ![i] = "activated"]
    /\ bus' = bus \cup {Ev("activated", i, cand[i], LenOf(cand[i]))}
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, er, cand, kept, CaptureVars, FoldVars, complete>>

\* The package is sent once activated is durable. With ActivateFirst off the
\* package can leave first and the orchestrator can die before activated.
Send(i) ==
    /\ \/ ph[i] = "activated"
       \/ ~ActivateFirst /\ ph[i] = "built"
    /\ ph' = [ph EXCEPT ![i] = "sent"]
    /\ complete' = IF CompleteOn = "sent" THEN complete \cup {i} ELSE complete
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, er, cand, kept, CaptureVars, bus, FoldVars>>

----------------------------------------------------------------------------
(* The edge (adapter) *)

\* The adapter checks the reassembled hash and its own invocation_id (a bad
\* package returns native, which EdgeTimeout covers) and returns install. A
\* precompute's package is kept by the harness for a later trigger.
EdgeInstall(i) ==
    /\ ph[i] = "sent"
    /\ er[i] = "wait"
    /\ er' = [er EXCEPT ![i] = "install"]
    /\ kept' = IF trig[i] = "precompute" THEN kept \cup {i} ELSE kept
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, ph, cand, CaptureVars, bus, FoldVars, complete>>

\* The edge misses its deadline; the orchestrator may still complete.
EdgeTimeout(i) ==
    /\ ph[i] \in {"started", "built", "activated", "sent"}
    /\ er[i] = "wait"
    /\ er' = [er EXCEPT ![i] = "native"]
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, ph, cand, kept, CaptureVars, bus, FoldVars, complete>>

----------------------------------------------------------------------------
(* Capture: late, in any order, lost, or truncated. A duplicate capture of a *)
(* record is the same position and changes nothing.                        *)

Capture(n) ==
    /\ n \in 1..Len(tr)
    /\ n \notin captured \cup dropped
    /\ captured' = captured \cup {n}
    /\ \E t \in BOOLEAN :
        /\ (t => tr[n].kind = "boundary")
        /\ trunc' = IF t THEN trunc \cup {n} ELSE trunc
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, ph, er, cand, kept, dropped, bus, FoldVars, complete>>

Drop(n) ==
    /\ n \in 1..Len(tr)
    /\ n \notin captured \cup dropped
    /\ Cardinality(dropped) < MaxDrops
    /\ dropped' = dropped \cup {n}
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, ph, er, cand, kept, captured, trunc, bus, FoldVars, complete>>

----------------------------------------------------------------------------
(* Install confirmation (TA.13): facts derived from captured records alone. *)
(* The activation line in a boundary names the activation it installed. An *)
(* activation is judged by the first boundary after its request's session  *)
(* high-water, read only through a gap-free captured run after that point. *)
(* That boundary is the only one that can install it: a boundary drops     *)
(* every kept precompute and revalidation refuses one built on older       *)
(* inputs, so the orchestrator needs no knowledge of the harness beyond    *)
(* the barrier its request carried.                                        *)

BoundaryFact(n) ==
    IF n \in trunc THEN "uncertain"
    ELSE IF tr[n].altered THEN "install_mismatch"
    ELSE "installed"

\* The end of the gap-free captured run of records after position p.
RunEnd(p) == CHOOSE q \in p..MaxLen : (p+1..q \subseteq captured) /\ (q + 1 \notin captured)

\* The fact for activation a whose request carried session high-water p.
Decide(a, p) ==
    LET B == {n \in p+1..RunEnd(p) : tr[n].kind = "boundary"} IN
    IF B # {} THEN
        LET b == CHOOSE n \in B : \A m \in B : n <= m IN
        IF b \in trunc THEN {"uncertain"}
        ELSE IF tr[b].act = a THEN {}                  \* the boundary fact covers it
        ELSE IF tr[b].act = None THEN {"native"}        \* not_installed(native_compaction)
        ELSE {"not_selected"}                           \* not_installed(not_selected)
    \* A later captured item and no boundary readable after the activation.
    ELSE IF GapRule /\ \E m \in captured : m > p THEN {"uncertain"}  \* capture_gap
    ELSE {}

Facts ==
    { Ev(BoundaryFact(n), tr[n].act, NoCand, 0) :
        n \in {m \in captured : tr[m].kind = "boundary" /\ tr[m].act # None} }
    \cup
    UNION { { Ev(f, e.a, NoCand, 0) : f \in Decide(e.a, e.x) } :
            e \in {x \in bus : x.k = "activated"} }

Confirm ==
    /\ ~(Facts \subseteq bus)
    /\ bus' = bus \cup Facts
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, ph, er, cand, kept, CaptureVars, FoldVars, complete>>

----------------------------------------------------------------------------
(* The fold: per activation, a function of the events received.            *)

Val(e) == e.k
CVal(e) == "h" \o ToString(e.x)

\* Invocations whose prepared or activated candidate is c, as the fold knows them.
ActsOf(c) == {a \in Inv : \E x \in seen : x.k = "activated" /\ x.a = a /\ x.c = c}

Deliver(e) ==
    /\ e \in bus
    /\ seen' = seen \cup {e}
    /\ IF e.k \in ActKinds
         THEN astate' = [astate EXCEPT ![e.a] =
                           IF FoldMode = "join" THEN Join(@, Val(e)) ELSE Val(e)]
         ELSE UNCHANGED astate
    /\ IF e.k = "prepared"
         THEN cstate' = [cstate EXCEPT ![e.c] =
                           IF FoldMode = "join" THEN CJoin(@, CVal(e)) ELSE CVal(e)]
         ELSE UNCHANGED cstate
    /\ IF e.k = "fallback"
         THEN istate' = [i \in Inv |->
                  IF i = e.a \/ (FallbackKey = "candidate" /\ e.c # NoCand /\ i \in ActsOf(e.c))
                  THEN "fallback" ELSE istate[i]]
         ELSE UNCHANGED istate
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, ph, er, cand, kept, CaptureVars, bus, complete>>

DeliverNew == \E e \in bus \ seen : Deliver(e)

\* Completion is reported from the fold, on an installed fact only.
Complete(a) ==
    /\ CompleteOn = "installed"
    /\ astate[a] = "installed"
    /\ a \notin complete
    /\ complete' = complete \cup {a}
    /\ UNCHANGED <<sv, overGap, tr, holding, trig, ph, er, cand, kept, CaptureVars, bus, FoldVars>>

----------------------------------------------------------------------------

Next ==
    \/ Turn
    \/ SourceChange
    \/ \E i \in Inv, t \in Triggers : Start(i, t)
    \/ \E i \in Inv : Compact(i) \/ Build(i) \/ Fail(i) \/ Activate(i) \/ Send(i)
                       \/ EdgeInstall(i) \/ EdgeTimeout(i) \/ Complete(i)
    \/ \E i, k \in Inv : UseKept(i, k)
    \/ \E n \in Positions : Capture(n) \/ Drop(n)
    \/ Confirm
    \/ \E e \in bus : Deliver(e)

Spec == Init /\ [][Next]_vars /\ WF_vars(Confirm) /\ WF_vars(DeliverNew)

----------------------------------------------------------------------------
(* Properties *)

\* No boundary installs an activation line whose activated event was never published.
NoInstallWithoutActivation ==
    \A n \in 1..Len(tr) :
        (tr[n].kind = "boundary" /\ tr[n].act # None)
            => \E e \in bus : e.k = "activated" /\ e.a = tr[n].act

\* The fold's state for each activation equals the join over the set of events
\* it has received, whatever their arrival order and however many duplicates.
SetFold(a) ==
    LET V == {Val(e) : e \in {x \in seen : x.k \in ActKinds /\ x.a = a}}
        top == {v \in V : \A w \in V : Rank(w) <= Rank(v)}
    IN IF V = {} THEN "none"
    ELSE IF Cardinality(top) = 1 THEN CHOOSE v \in top : TRUE
    ELSE "conflict"
SetFoldC(c) ==
    LET V == {CVal(e) : e \in {x \in seen : x.k = "prepared" /\ x.c = c}} IN
    IF V = {} THEN "none" ELSE IF Cardinality(V) = 1 THEN CHOOSE v \in V : TRUE ELSE "conflict"
SetFoldI(i) ==
    IF \E e \in seen : e.k = "fallback" /\ e.a = i THEN "fallback" ELSE "none"
OneStatePerIdentityUnderAnyArrivalOrder ==
    /\ \A a \in Inv : astate[a] = SetFold(a)
    /\ \A c \in Cands : cstate[c] = SetFoldC(c)
    /\ \A i \in Inv : istate[i] = SetFoldI(i)

\* Every trigger: a boundary installs only a candidate whose whole version
\* vector equals the one its trigger fixed, and that replaces the records the
\* boundary follows.
StaleCandidateNeverInstalled ==
    \A n \in 1..Len(tr) :
        (tr[n].kind = "boundary" /\ tr[n].act # None) =>
            /\ cand[tr[n].act] = tr[n].tv
            /\ LenOf(cand[tr[n].act]) = n - 1

\* The fold records fallback only for the invocation that fell back, never for
\* another invocation that shares its candidate.
FallbackNeverConflictsWithAnotherInvocation ==
    \A i \in Inv : istate[i] = "fallback" => \E e \in bus : e.k = "fallback" /\ e.a = i

\* No candidate is built over incomplete sources.
NoBuildOverIncompleteSources == overGap = {}

\* Nothing is reported complete before an installed fact exists for it.
NoCompleteBeforeInstalled ==
    \A a \in complete : Ev("installed", a, NoCand, 0) \in bus

\* Liveness: an activation whose session's captured stream advances past its
\* request's high-water reaches installed, install_mismatch, not_installed or uncertain.
Advanced(a) ==
    \E e \in bus : e.k = "activated" /\ e.a = a /\ \E m \in captured : m > e.x
EveryActivationResolves ==
    \A a \in Inv : Advanced(a) ~> (astate[a] \in Resolved)

=============================================================================
