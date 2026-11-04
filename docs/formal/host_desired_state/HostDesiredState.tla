------------------------- MODULE HostDesiredState ---------------------------
(* Durable publisher state and outbox, with bounded, atomic scenario scheduling.
   The declared publisher is on h1; another publisher on h2 uses the same producer
   identity. Placement probes use hosts hA and hB.

   Each pass has eight separately crashable steps:
     1 finish pending first; 2 fetch; 3 read requests in topic order; 4 compute;
     5 durably write pending (record bytes, tags, decisions, consumed ids, core);
     6 push tags; 7 publish and await broker acceptance; 8 commit the pending core.
   A remote tag is accepted only when its commit equals the recorded commit.
   A crash loses memory, never the state file. Recovery opens a newer producer.
   One send may remain delayed across recovery until after generation 3. The
   second publisher also opens a newer producer, fencing the declared publisher.

   Invariants and the single constant flipped by each mutant:
     GenerationMonotone: declared-publisher records in broker order never decrease
       in generation. Fenced=FALSE permits a delayed, older producer's delivery.
     OnePublisherAccepted: the reader holds only the declared publisher's record.
       ReaderFilters=FALSE admits the other publisher into the generation fold.
     PendingTagsBeforePublish: every declared-publisher delivery had all recorded
       tags at their exact commits then. TagsFirst=FALSE delays pushing to commit.
     PendingRepublishedExactly: every declared-publisher bus record equals the
       FIRST durable pending record for its generation, and every committed core
       equals that first pending core. ResumeExact=FALSE recomputes on recovery.
     ReleasedLayerNeverReturns: a released id cannot be live at a strictly later
       generation, including releases in durable tombstone history. Tombstones=
       FALSE omits the durable tombstone guard and permits a fresh registration.
     DependentLeavesWithDependency: in every accepted record each live layer's
       dependency is live, merged into the base (the dependent then survives: the
       base holds the dependency's change), or was never registered; a dependency
       that left for any other reason (withdrawn, expired, dependency_released) is
       in the cumulative gone set and takes its dependents with it, transitively.
       Cascade=FALSE disables the transitive cascade only; the register-time
       refusal of a layer whose dependency is already gone stays on.
     StaleRequestNeverApplies: each applied request names the live revision at
       application. CheckRevision=FALSE accepts a stale extend/withdraw revision.
     NoLaunchOnExpiredLayer: no admitted lane used an expired held record or a
       host answering a different generation. CheckValidUntil=FALSE checks only
       host generation, allowing an expired record while the publisher is stopped.

   Limits:
   Three layers l1 <- l2 <- l3; three generations; one crash, at any of the eight
   steps in any pass (step 7 before OR after delivery); bounded time. Each script
   fixes arrivals, merge and time at pass boundaries. Both orders of the same-
   revision extend/withdraw race, duplicates and dependency-first reordering are
   explicit scripts. The resume script adds a request and advances time while
   pending is durable. The stopped script has NO Boot after Crash and probes
   both hosts before, at and after expiry. hB answers one generation behind.
   The lifecycle register/release/attempted-resurrection spans generations 1/2/3.
   These scripts replace unbounded environment interleavings, not the listed
   races. No liveness, arbitrary network loss, or unbounded producer population.
   A record value abstracts canonical serialized bytes; a commit is a symbolic
   tuple. Matching and conflicting preexisting tags have separate scripts.
   Histories and error flags are observers, never inputs to publisher decisions.
   Initial choices are bounded and subsequent scheduling is deterministic;
   there are no freely interleaving clock, request, reader or second-publisher
   actions. MaxGen=3 covers the lifecycle without an exponential pass scheduler.
   With the supplied bounds there are 365 initial schedules, each with fewer
   than 80 transitions, bounding the design below 30,000 reachable states.
*)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS MaxGen, MaxCrash, MaxT,
          Fenced, ReaderFilters, TagsFirst, ResumeExact, Tombstones, Cascade,
          CheckRevision, CheckValidUntil

Layers == {"l1", "l2", "l3"}
Hosts == {"hA", "hB"}
Dep == [l1 |-> "none", l2 |-> "l1", l3 |-> "l2"]
Lifetime == [l1 |-> 2, l2 |-> 3, l3 |-> 4]
Kind == [rg1 |-> "register", rg2 |-> "register", rg3 |-> "register",
         rg1b |-> "register", ex1 |-> "extend", ex2 |-> "extend", wd1 |-> "withdraw"]
LayerOf == [rg1 |-> "l1", rg2 |-> "l2", rg3 |-> "l3", rg1b |-> "l1",
            ex1 |-> "l1", ex2 |-> "l2", wd1 |-> "l1"]
ExpRev == [r \in DOMAIN Kind |-> IF Kind[r] = "register" THEN 0 ELSE 1]
NONE == [gen |-> 0]
NoRel == [l \in Layers |-> "none"]
INF == MaxT + 5
Max(a, b) == IF a >= b THEN a ELSE b

Batch(rs, t, merged) == [reqs |-> rs, time |-> t, merged |-> merged]
Stack == <<"rg1", "rg2", "rg3">>
Scripts ==
  [withdraw |-> <<Batch(Stack, 0, {}), Batch(<<"wd1">>, 0, {}), Batch(<<"rg1b">>, 0, {})>>,
   expiry |-> <<Batch(Stack, 0, {}), Batch(<<>>, 2, {}), Batch(<<"rg1b">>, 2, {})>>,
   extended |-> <<Batch(Stack, 0, {}), Batch(<<"ex2">>, 1, {}), Batch(<<>>, 2, {})>>,
   late |-> <<Batch(<<"rg1">>, 0, {}), Batch(<<"wd1">>, 0, {}), Batch(<<"rg2", "rg3">>, 0, {})>>,
   merged |-> <<Batch(Stack, 0, {}), Batch(<<>>, 0, {"l1"}), Batch(<<>>, 3, {"l1"})>>,
   extendWithdraw |-> <<Batch(<<"rg1">>, 0, {}), Batch(<<"ex1", "wd1">>, 0, {}), Batch(<<>>, 3, {})>>,
   withdrawExtend |-> <<Batch(<<"rg1">>, 0, {}), Batch(<<"wd1", "ex1">>, 0, {}), Batch(<<"rg1b">>, 0, {})>>,
   reordered |-> <<Batch(<<"rg3", "rg2", "rg1", "rg1">>, 0, {}), Batch(<<"wd1">>, 0, {}), Batch(<<>>, 0, {})>>,
   resume |-> <<Batch(<<"rg1">>, 0, {}), Batch(<<"ex1">>, 1, {}), Batch(<<>>, 2, {})>>,
   otherEarly |-> <<Batch(Stack, 0, {}), Batch(<<>>, 0, {}), Batch(<<>>, 0, {})>>,
   otherLate |-> <<Batch(Stack, 0, {}), Batch(<<>>, 0, {}), Batch(<<>>, 0, {})>>,
   tagMatch |-> <<Batch(Stack, 0, {}), Batch(<<>>, 0, {}), Batch(<<>>, 0, {})>>,
   tagConflict |-> <<Batch(Stack, 0, {}), Batch(<<>>, 0, {}), Batch(<<>>, 0, {})>>,
   stopped |-> <<Batch(Stack, 0, {}), Batch(<<>>, 0, {}), Batch(<<>>, 0, {})>>]

Faults == {[step |-> 0, pass |-> 1, afterDelivery |-> FALSE]}
          \cup {[step |-> s, pass |-> g, afterDelivery |-> FALSE] : s \in 1..8, g \in 1..MaxGen}
          \cup {[step |-> 7, pass |-> g, afterDelivery |-> TRUE] : g \in 1..MaxGen}

\* All operational fields are in v. EXCEPT preserves every unmentioned field.
\* Histories are separate so their UNCHANGED clauses can be checked directly.
VARIABLES scn, fault, v, pendingHistory, commitHistory, tombHistory
vars == <<scn, fault, v, pendingHistory, commitHistory, tombHistory>>
S == v.state
EmptyCore == [live |-> {}, rev |-> [l \in Layers |-> 0], exp |-> [l \in Layers |-> 0],
              tomb |-> {}, gone |-> {}, merged |-> {}, consumed |-> {}, decisions |-> {}, off |-> 0]

Leave(W, leaving, why) ==
  [W EXCEPT !.live = @ \ leaving,
            !.tomb = IF Tombstones THEN @ \cup leaving ELSE @,
            !.gone = IF why = "merged" THEN @ ELSE @ \cup leaving,
            !.merged = IF why = "merged" THEN @ \cup leaving ELSE @,
            !.rel = [l \in Layers |-> IF l \in leaving THEN why ELSE W.rel[l]]]

Note(W, r, outcome, reason) ==
  [W EXCEPT !.consumed = @ \cup {r},
            !.decisions = @ \cup {[id |-> r, outcome |-> outcome, reason |-> reason,
                                   named |-> ExpRev[r],
                                   at |-> IF LayerOf[r] \in W.live THEN W.rev[LayerOf[r]] ELSE 0]}]

ApplyReq(W, r, t) ==
  LET l == LayerOf[r]
      okRev == ~CheckRevision \/ W.rev[l] = ExpRev[r]
      applied == Note(W, r, "applied", Kind[r])
      refused == Note(W, r, "refused", "stale_or_released")
  IN IF r \in W.consumed THEN W
     ELSE CASE Kind[r] = "register" ->
            IF Dep[l] \in W.gone
              THEN Leave(Note(W, r, "refused", "dependency_released"), {l}, "dependency_released")
            ELSE IF l \notin W.live /\ (~Tombstones \/ l \notin W.tomb)
              THEN [applied EXCEPT !.live = @ \cup {l}, !.rev[l] = 1,
                                   !.exp[l] = t + Lifetime[l]]
              ELSE refused
          [] Kind[r] = "extend" ->
            IF l \in W.live /\ okRev
              THEN [applied EXCEPT !.rev[l] = @ + 1, !.exp[l] = @ + 1]
              ELSE refused
          [] OTHER -> IF l \in W.live /\ okRev THEN Leave(applied, {l}, "withdrawn") ELSE refused

RECURSIVE Fold(_, _, _)
Fold(W, rs, t) == IF rs = <<>> THEN W ELSE Fold(ApplyReq(W, Head(rs), t), Tail(rs), t)

\* At most |Layers| iterations reaches the dependency fixpoint, including l3.
RECURSIVE Close(_, _)
Close(W, n) ==
  IF n = 0 THEN W
  ELSE LET leaving == {l \in W.live : Dep[l] \in W.gone}
       IN Close(Leave(W, leaving, "dependency_released"), n - 1)

MinExp(W) == IF W.live = {} THEN INF
            ELSE CHOOSE m \in {W.exp[l] : l \in W.live} : \A l \in W.live : m <= W.exp[l]

Compute(core, rs, off, t, merged, g) ==
  LET W0 == [core EXCEPT !.off = off]
      W1 == Fold([live |-> W0.live, rev |-> W0.rev, exp |-> W0.exp,
                  tomb |-> W0.tomb, gone |-> W0.gone, merged |-> W0.merged, consumed |-> W0.consumed,
                  decisions |-> W0.decisions, off |-> W0.off, rel |-> NoRel], rs, t)
      W2 == Leave(W1, W1.live \cap merged, "merged")
      W3 == Leave(W2, {l \in W2.live : W2.exp[l] <= t}, "expired")
      W4 == IF Cascade THEN Close(W3, Cardinality(Layers)) ELSE W3
      coreOut == [live |-> W4.live, rev |-> W4.rev, exp |-> W4.exp, tomb |-> W4.tomb,
                  gone |-> W4.gone, merged |-> W4.merged, consumed |-> W4.consumed, decisions |-> W4.decisions, off |-> off]
      tag == [name |-> g, commit |-> <<g, W4.live, W4.rev, W4.exp>>]
      rec == [gen |-> g, pub |-> "h1", live |-> W4.live, gone |-> W4.gone, merged |-> W4.merged, tomb |-> W4.tomb,
              rel |-> W4.rel, valid_until |-> MinExp(W4), tag |-> tag,
              decisions |-> W4.decisions, consumed |-> W4.consumed]
  IN [gen |-> g, core |-> coreOut, rec |-> rec]

Init ==
  /\ scn \in DOMAIN Scripts
  /\ fault \in IF scn = "stopped"
                 THEN {[step |-> 8, pass |-> 1, afterDelivery |-> FALSE]} ELSE Faults
  /\ v = [state |-> [hw |-> 0, core |-> EmptyCore, pending |-> NONE],
           pass |-> 1, pc |-> 0, mem |-> NONE, memOff |-> 0,
           alive |-> TRUE, stopped |-> FALSE, crashes |-> 0,
           epoch |-> 1, producerEpoch |-> 1, otherEpoch |-> 0, otherStage |-> 0,
           topic |-> Scripts[scn][1].reqs, now |-> 0, merged |-> {},
           remote |-> [g \in 1..MaxGen |-> <<>>],
           inflight |-> NONE, deferred |-> NONE, bus |-> <<>>,
           probe |-> 0, obs |-> [h \in Hosts |-> 0], badLaunch |-> FALSE, badTag |-> FALSE]
  /\ pendingHistory = [g \in 1..MaxGen |-> NONE]
  /\ commitHistory = <<>>
  /\ tombHistory = [g \in 1..MaxGen |-> {}]

CrashDue ==
  /\ v.crashes < MaxCrash /\ fault.step # 0
  /\ v.pass = fault.pass /\ v.pc = fault.step
  /\ IF fault.step = 7 THEN (v.inflight = NONE) = fault.afterDelivery ELSE TRUE
Ready == v.alive /\ v.probe = 0 /\ ~CrashDue

Crash ==
  /\ v.alive /\ v.probe = 0 /\ CrashDue
  /\ v' = [v EXCEPT !.alive = FALSE, !.stopped = (scn = "stopped"),
                    !.crashes = @ + 1, !.mem = NONE, !.memOff = 0,
                    !.pc = IF v.pc = 8 THEN 8 ELSE 0,
                    !.deferred = IF v.inflight # NONE THEN v.inflight ELSE @,
                    !.inflight = NONE,
                    !.topic = IF scn = "resume" /\ v.pass = 1 /\ S.pending # NONE
                                THEN Append(@, "rg2") ELSE @,
                    !.now = IF scn = "resume" /\ v.pass = 1 /\ S.pending # NONE THEN 1 ELSE @]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

Boot ==
  /\ ~v.alive /\ ~v.stopped
  /\ v' = [v EXCEPT !.alive = TRUE, !.epoch = @ + 1, !.producerEpoch = v.epoch + 1]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

\* A fenced publisher reconnects before its next pass (the second publisher has
\* already delivered its one message). This does not change the durable outbox.
Reconnect ==
  /\ Ready /\ v.pc = 0 /\ v.producerEpoch # v.epoch
  /\ v' = [v EXCEPT !.epoch = @ + 1, !.producerEpoch = v.epoch + 1]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

Start ==
  /\ Ready /\ v.pc = 0 /\ v.producerEpoch = v.epoch
  /\ S.pending # NONE \/ S.hw < MaxGen
  /\ v' = IF S.pending # NONE /\ ResumeExact
            THEN [v EXCEPT !.mem = S.pending, !.pc = 5]
            ELSE [v EXCEPT !.mem = NONE, !.pc = 1]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

Fetch ==
  /\ Ready /\ v.pc = 1 /\ v' = [v EXCEPT !.pc = 2]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

ReadTopic ==
  /\ Ready /\ v.pc = 2 /\ v' = [v EXCEPT !.memOff = Len(v.topic), !.pc = 3]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

ComputeStep ==
  /\ Ready /\ v.pc = 3
  /\ LET g == IF S.pending = NONE THEN S.hw + 1 ELSE S.pending.gen
     IN /\ g <= MaxGen
        /\ v' = [v EXCEPT !.mem = Compute(S.core, SubSeq(v.topic, S.core.off + 1, v.memOff),
                                         v.memOff, v.now, v.merged, g), !.pc = 4]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

WritePending ==
  /\ Ready /\ v.pc = 4
  /\ LET g == v.mem.gen
         seed == v.pass = 1 /\ v.remote[g] = <<>>
     IN /\ v' = [v EXCEPT !.state.hw = g, !.state.pending = v.mem, !.pc = 5,
                          !.remote[g] = IF seed /\ scn = "tagMatch" THEN v.mem.rec.tag.commit
                                        ELSE IF seed /\ scn = "tagConflict" THEN <<"unknown writer">>
                                        ELSE @]
        /\ pendingHistory' = IF pendingHistory[g] = NONE
                               THEN [pendingHistory EXCEPT ![g] = v.mem] ELSE pendingHistory
        /\ tombHistory' = [tombHistory EXCEPT ![g] = @ \cup v.mem.core.tomb
                                             \cup {l \in Layers : v.mem.rec.rel[l] # "none"}]
  /\ UNCHANGED <<scn, fault, commitHistory>>

\* All remote values are tuples; <<>> is absent, avoiding mixed-type equality.
TagCompatible == v.remote[v.mem.gen] \in {<<>>, v.mem.rec.tag.commit}
TagsPresent(rec) == v.remote[rec.tag.name] = rec.tag.commit

PushTags ==
  /\ Ready /\ v.pc = 5
  /\ ~TagsFirst \/ TagCompatible
  /\ v' = [v EXCEPT !.remote[v.mem.gen] = IF TagsFirst THEN v.mem.rec.tag.commit ELSE @,
                    !.pc = 6]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

Send ==
  /\ Ready /\ v.pc = 6
  /\ v' = [v EXCEPT !.inflight = [gen |-> v.mem.gen, rec |-> v.mem.rec, ep |-> v.producerEpoch], !.pc = 7]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

Deliver ==
  /\ Ready /\ v.pc = 7 /\ v.inflight # NONE
  /\ LET m == v.inflight
         accept == ~Fenced \/ m.ep = v.epoch
     IN v' = [v EXCEPT !.inflight = NONE,
                        !.bus = IF accept THEN Append(@, m) ELSE @,
                        !.badTag = @ \/ (accept /\ ~TagsPresent(m.rec)),
                        !.probe = IF accept THEN 1 ELSE 0]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

\* Scheduled between the declared publisher's delivery and commit, once only.
OtherDue == v.otherStage = 0 /\ v.pc = 7 /\ v.inflight = NONE
            /\ ((scn = "otherEarly" /\ v.pass = 1) \/ (scn = "otherLate" /\ v.pass = 2))
OpenOther ==
  /\ Ready /\ OtherDue
  /\ v' = [v EXCEPT !.epoch = @ + 1, !.otherEpoch = v.epoch + 1, !.otherStage = 1]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

PublishOther ==
  /\ Ready /\ v.otherStage = 1
  /\ LET g == IF scn = "otherEarly" THEN 2 ELSE 1
         rec == [gen |-> g, pub |-> "h2", live |-> {}, gone |-> {}, merged |-> {}, tomb |-> {},
                 rel |-> NoRel, valid_until |-> INF,
                 tag |-> [name |-> g, commit |-> <<"unknown writer">>], decisions |-> {}, consumed |-> {}]
     IN v' = [v EXCEPT !.bus = Append(@, [gen |-> g, rec |-> rec, ep |-> v.otherEpoch]),
                        !.otherStage = 2, !.probe = 1]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

Commit ==
  /\ Ready /\ v.pc = 7 /\ v.inflight = NONE /\ ~OtherDue /\ v.otherStage # 1
  /\ \E i \in DOMAIN v.bus : v.bus[i].rec = v.mem.rec /\ v.bus[i].ep = v.producerEpoch
  /\ ~TagsFirst => TagCompatible
  /\ v' = [v EXCEPT !.state.core = v.mem.core, !.state.pending = NONE,
                    !.remote[v.mem.gen] = IF TagsFirst THEN @ ELSE v.mem.rec.tag.commit,
                    !.mem = NONE, !.pc = 8]
  /\ commitHistory' = Append(commitHistory, [gen |-> v.mem.gen, core |-> v.mem.core])
  /\ UNCHANGED <<scn, fault, pendingHistory, tombHistory>>

Advance ==
  /\ Ready /\ v.pc = 8 /\ v.pass < MaxGen /\ v.pass < Len(Scripts[scn])
  /\ LET b == Scripts[scn][v.pass + 1]
     IN v' = [v EXCEPT !.pass = @ + 1, !.pc = 0, !.topic = @ \o b.reqs,
                        !.now = Max(@, b.time), !.merged = b.merged, !.probe = 1]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

\* The old generation is delivered after newer records, never folded by offset.
DeliverDeferred ==
  /\ Ready /\ v.pc = 8 /\ v.pass = MaxGen /\ v.deferred # NONE
  /\ LET m == v.deferred
         accept == ~Fenced \/ m.ep = v.epoch
     IN v' = [v EXCEPT !.deferred = NONE,
                        !.bus = IF accept THEN Append(@, m) ELSE @,
                        !.badTag = @ \/ (accept /\ ~TagsPresent(m.rec)),
                        !.probe = IF accept THEN 1 ELSE 0]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

IsAccepted(m) == ~ReaderFilters \/ m.rec.pub = "h1"
AccSeq == SelectSeq(v.bus, IsAccepted)
Declared(m) == m.rec.pub = "h1"
DeclaredSeq == SelectSeq(v.bus, Declared)

RECURSIVE Hold(_, _)
Hold(ms, held) ==
  IF ms = <<>> THEN held
  ELSE Hold(Tail(ms), IF Head(ms).rec.gen > held.gen THEN Head(ms).rec ELSE held)
HeldRec == Hold(AccSeq, NONE)

\* Each probe reconciles hA to the held generation and leaves hB one behind,
\* then attempts placement on BOTH. A refused attempt is an explicit transition.
Observe ==
  /\ v.probe = 1
  /\ v' = [v EXCEPT !.obs = [h \in Hosts |-> IF h = "hA" THEN HeldRec.gen ELSE Max(0, HeldRec.gen - 1)],
                    !.probe = 2]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

TryLaunch(h, nextProbe) ==
  LET exists == HeldRec # NONE
      generationOK == v.obs[h] = HeldRec.gen
      timeOK == IF exists THEN v.now < HeldRec.valid_until ELSE FALSE
      admitted == exists /\ generationOK /\ (~CheckValidUntil \/ timeOK)
  IN /\ v' = [v EXCEPT !.badLaunch = @ \/ (admitted /\ (~timeOK \/ ~generationOK)), !.probe = nextProbe]
     /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

ProbeLaunch == (v.probe = 2 /\ TryLaunch("hA", 3))
               \/ (v.probe = 3 /\ TryLaunch("hB", 0))

TickStopped ==
  /\ v.stopped /\ ~v.alive /\ v.probe = 0 /\ v.now < MaxT
  /\ v' = [v EXCEPT !.now = @ + 1, !.probe = 1]
  /\ UNCHANGED <<scn, fault, pendingHistory, commitHistory, tombHistory>>

Next == Crash \/ Boot \/ Reconnect \/ Start \/ Fetch \/ ReadTopic \/ ComputeStep
        \/ WritePending \/ PushTags \/ Send \/ Deliver \/ OpenOther \/ PublishOther
        \/ Commit \/ Advance \/ DeliverDeferred \/ Observe \/ ProbeLaunch \/ TickStopped
Spec == Init /\ [][Next]_vars

\* Mutation isolation: broker-order checks exclude h2; release comparisons use
\* strictly increasing generations, so replaying g1 after g3 is not resurrection.
\* The only changed recovery inputs are at g1 in resume, before any release;
\* recomputing there cannot lose an earlier tombstone. The other publisher has
\* no live layers. Cascade, revision and tombstone mutations still use the exact
\* outbox and the time gate; tag and time errors are independent history flags.
\* None of these observers is read by an action to suppress another failure.
TypeOK ==
  /\ S.hw \in 0..MaxGen /\ v.pass \in 1..MaxGen /\ v.pc \in 0..8
  /\ v.now \in 0..MaxT /\ v.crashes \in 0..MaxCrash
  /\ DOMAIN v.obs = Hosts /\ DOMAIN v.remote = 1..MaxGen
  /\ S.core.live \subseteq Layers /\ S.core.gone \subseteq Layers
  /\ DOMAIN pendingHistory = 1..MaxGen /\ DOMAIN tombHistory = 1..MaxGen

GenerationMonotone ==
  \A i, j \in DOMAIN DeclaredSeq : i < j => DeclaredSeq[i].rec.gen <= DeclaredSeq[j].rec.gen

OnePublisherAccepted == HeldRec = NONE \/ HeldRec.pub = "h1"
PendingTagsBeforePublish == ~v.badTag

PendingRepublishedExactly ==
  /\ \A i \in DOMAIN DeclaredSeq :
       LET rec == DeclaredSeq[i].rec
       IN pendingHistory[rec.gen] # NONE /\ rec = pendingHistory[rec.gen].rec
  /\ \A i \in DOMAIN commitHistory :
       LET c == commitHistory[i]
       IN pendingHistory[c.gen] # NONE /\ c.core = pendingHistory[c.gen].core

ReleasedLayerNeverReturns ==
  /\ \A i, j \in DOMAIN AccSeq : AccSeq[i].rec.gen < AccSeq[j].rec.gen =>
       {l \in Layers : AccSeq[i].rec.rel[l] # "none"} \cap AccSeq[j].rec.live = {}
  /\ \A g \in 1..MaxGen : \A i \in DOMAIN AccSeq :
       g < AccSeq[i].rec.gen => tombHistory[g] \cap AccSeq[i].rec.live = {}

NeverRegistered(rec, d) == d \notin (rec.live \cup rec.gone \cup rec.merged)
DependentLeavesWithDependency ==
  \A i \in DOMAIN AccSeq : LET rec == AccSeq[i].rec IN
    /\ \A l \in rec.live : Dep[l] = "none" \/ Dep[l] \in rec.live \cup rec.merged
                            \/ NeverRegistered(rec, Dep[l])
    /\ rec.merged \subseteq v.merged        \* a merge release rests on the fetched base holding the change

StaleRequestNeverApplies ==
  /\ \A d \in S.core.decisions : d.outcome = "applied" => d.named = d.at
  /\ S.pending # NONE =>
       \A d \in S.pending.core.decisions : d.outcome = "applied" => d.named = d.at

NoLaunchOnExpiredLayer == ~v.badLaunch
=============================================================================
