------------------------- MODULE HostDesiredState ---------------------------
(* The desired-state publisher of the host code-deploy mechanism: its durable state
   file and outbox (the eight steps of one pass, with a crash after each), the layer
   requests it folds, the layers it releases, the broker that fences older producers,
   a second publisher on another host, the readers that fold what the topic holds, and
   the placement gate that admits a lane on a host that answers the held generation.

   One publisher host is declared in the overlay (here "h1", process pa). A second
   process pb on another host ("h2") is a misconfigured copy: it opens a producer under
   the same transactional id (fencing pa) and may send one record of its own.

   The durable state file S holds the generation high-water mark hw, the live layers
   with their revisions and expiries, the tombstones of released layers, the request ids
   consumed with their outcomes (applied log), the topic offset read, and one pending
   generation. A pass:
     1 if a pending generation exists finish it (steps 6 to 8) before reading anything
     2 fetch   3 read the layer requests from the topic offset   4 compute
     5 write the result as pending (exact record, tag, consumed ids)
     6 push the tag   7 publish the record   8 mark committed
   A crash may come after any step; the process then boots again and opens a newer
   producer, which fences every older one at the broker.

   Requests for one layer arrive in any order, may be duplicated, and the extend and the
   withdraw both name revision 1 (a race): the topic order decides, the second one names
   a stale revision. The reader folds records by generation, never by offset, keeps the
   newest, and ignores a record whose publisher host is not the declared one.

   Invariants (each broken by its own mutant, a flipped constant of Model.cfg):
     GenerationMonotone         accepted records never go back in generation
                                (Fenced = FALSE: the broker does not fence older producers)
     OnePublisherAccepted       every accepted record comes from the declared publisher host
                                (ReaderFilters = FALSE: the reader accepts any publisher host)
     PendingTagsBeforePublish   a published record's tag is already on the remote
                                (TagsFirst = FALSE: the tag is pushed at commit, after publish)
     PendingRepublishedExactly  a resumed pending generation is published with its exact
                                bytes: records of one generation are identical
                                (ResumeExact = FALSE: the resume recomputes)
     ReleasedLayerNeverReturns  a layer released in one record is live in no later one
                                (Tombstones = FALSE: a released layer id can register again)
     DependentLeavesWithDependency  a stacked layer does not outlive its dependency
                                (Cascade = FALSE: dependents are not released with it)
     StaleRequestNeverApplies   a request applies only at the revision it names
                                (CheckRevision = FALSE: the revision is not compared)
     NoLaunchOnExpiredLayer     no lane is admitted past the held record's valid_until,
                                or on a host that does not answer the held generation
                                (CheckValidUntil = FALSE: only the generation is compared) *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS MaxGen, MaxCrash, MaxT,           Fenced, ReaderFilters, TagsFirst, ResumeExact, Tombstones, Cascade,
          CheckRevision, CheckValidUntil

Layers == {"l1", "l2"}
Hosts  == {"hA"}                       \* a host whose reconciler answers a generation
Reqs   == {"rg1", "rg2", "ex1", "wd1", "rg1b"}
Kind   == [rg1 |-> "register", rg2 |-> "register", ex1 |-> "extend",
           wd1 |-> "withdraw", rg1b |-> "register"]
LayerOf == [rg1 |-> "l1", rg2 |-> "l2", ex1 |-> "l1", wd1 |-> "l1", rg1b |-> "l1"]
ExpRev == [rg1 |-> 0, rg2 |-> 0, ex1 |-> 1, wd1 |-> 1, rg1b |-> 0]
Dep == [l1 |-> "none", l2 |-> "l1"]    \* l2 carries commits of l1 that the base lacks
INF == MaxT + 3
\* Orders in which requests reach the topic: a cascade by withdraw, an extend racing a
\* withdraw on revision 1 (both orders), a register of a released id, a duplicate, a
\* reordered pair (the dependent first), expiry of stacked layers, a lone layer.
Scenarios == {<<"rg1", "rg2", "wd1">>, <<"rg1", "ex1", "wd1">>, <<"rg1", "wd1", "ex1">>,
              <<"rg1", "wd1", "rg1b">>, <<"rg1", "rg1", "wd1">>, <<"rg2", "rg1", "wd1">>,
              <<"rg1", "rg2", "ex1">>, <<"rg1", "rg2">>, <<"rg1">>}
NONE == [gen |-> 0]            \* no pending generation / no result in memory
NoRel == [l \in Layers |-> "none"]

VARIABLES scn,        \* the scenario: the order in which the requests reach the topic
          S,          \* the publisher's durable state file
          mem,        \* the publisher's in-memory result of step 4 (lost on a crash)
          memOff,     \* topic offset read in step 3
          pc, paAlive, paEpoch, crashes,
          remoteTags, \* tags on the remote git host (generation numbers)
          topic,      \* the layer request topic
          inflight,   \* records sent and not yet delivered or dropped by the broker
          bus,        \* the desired-state topic: <<[rec, ep]>> in broker order
          epoch,      \* the broker's current producer epoch for the transactional id
          pbAlive, pbEpoch, pbSent,
          mergedSet, now, obs, badLaunch, scn

vars == <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags, topic, inflight,
          bus, epoch, pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

EmptyCore == [live |-> {}, rev |-> [l \in Layers |-> 0], exp |-> [l \in Layers |-> 0],
              tomb |-> {}, consumed |-> {}, applied |-> {}, off |-> 0]
InitS == [hw |-> 0, core |-> EmptyCore, pending |-> NONE]


\* ---- pure compute (step 4) -------------------------------------------------
\* W is the working record: the core plus rel, the layers released in this pass.
Leave(W, Sx, why) ==
  [W EXCEPT !.live = W.live \ Sx,
            !.tomb = IF Tombstones THEN W.tomb \cup Sx ELSE W.tomb,
            !.rel  = [l \in Layers |-> IF l \in Sx THEN why ELSE W.rel[l]]]

ApplyReq(W, r, nowv) ==
  LET l == LayerOf[r]
      fresh == r \notin W.consumed
      okRev == IF CheckRevision THEN W.rev[l] = ExpRev[r] ELSE TRUE
      revAt == IF l \in W.live THEN W.rev[l] ELSE 0
      note == [W EXCEPT !.consumed = W.consumed \cup {r},
                        !.applied = W.applied \cup {[id |-> r, named |-> ExpRev[r], at |-> revAt]}]
      noted == [W EXCEPT !.consumed = W.consumed \cup {r}]
  IN IF ~fresh THEN W            \* a request id seen before returns its recorded outcome
     ELSE CASE Kind[r] = "register" ->
                 IF l \notin W.live /\ (~Tombstones \/ l \notin W.tomb) /\ ExpRev[r] = 0
                   THEN [note EXCEPT !.live = W.live \cup {l},
                                     !.rev = [W.rev EXCEPT ![l] = 1],
                                     !.exp = [W.exp EXCEPT ![l] = nowv + 1]]
                   ELSE noted
            [] Kind[r] = "extend" ->
                 IF l \in W.live /\ okRev
                   THEN [note EXCEPT !.rev = [W.rev EXCEPT ![l] = @ + 1],
                                     !.exp = [W.exp EXCEPT ![l] = @ + 1]]
                   ELSE noted
            [] OTHER ->          \* withdraw
                 IF l \in W.live /\ okRev
                   THEN Leave(note, {l}, "withdrawn")
                   ELSE noted

RECURSIVE Fold(_, _, _)
Fold(W, reqs, nowv) == IF reqs = <<>> THEN W
                       ELSE Fold(ApplyReq(W, Head(reqs), nowv), Tail(reqs), nowv)

MinExp(W) == IF W.live = {} THEN INF
             ELSE CHOOSE m \in {W.exp[l] : l \in W.live} : \A x \in {W.exp[l] : l \in W.live} : m <= x

Compute(core, reqs, newOff, nowv, merged, gen) ==
  LET W0 == [live |-> core.live, rev |-> core.rev, exp |-> core.exp, tomb |-> core.tomb,
             consumed |-> core.consumed, applied |-> core.applied, rel |-> NoRel]
      W1 == Fold(W0, reqs, nowv)
      W2 == Leave(W1, W1.live \cap merged, "merged")
      W3 == Leave(W2, {l \in W2.live : W2.exp[l] <= nowv}, "expired")
      dependents == {x \in W3.live : Dep[x] # "none" /\ W3.rel[Dep[x]] \in
                      {"withdrawn", "expired", "dependency_released"}}
      W4 == IF Cascade THEN Leave(W3, dependents, "dependency_released") ELSE W3
      changed == reqs # <<>> \/ \E l \in Layers : W4.rel[l] # "none"
      newCore == [live |-> W4.live, rev |-> W4.rev, exp |-> W4.exp, tomb |-> W4.tomb,
                  consumed |-> W4.consumed, applied |-> W4.applied, off |-> newOff]
      rec == [gen |-> gen, pub |-> "h1", live |-> W4.live, rel |-> W4.rel,
              vu |-> MinExp(W4), tag |-> gen]
  IN [changed |-> changed, p |-> [gen |-> gen, core |-> newCore, rec |-> rec]]

\* ---- initial state ----------------------------------------------------------
Init ==
  /\ scn \in Scenarios
  /\ S = InitS /\ mem = NONE /\ memOff = 0
  /\ pc = "idle" /\ paAlive = TRUE /\ paEpoch = 1 /\ crashes = 0
  /\ remoteTags = {} /\ topic = <<>> /\ inflight = {} /\ bus = <<>>
  /\ epoch = 1 /\ pbAlive = FALSE /\ pbEpoch = 0 /\ pbSent = FALSE
  /\ mergedSet = {} /\ now = 0 /\ obs = [h \in Hosts |-> 0] /\ badLaunch = FALSE

\* ---- the environment --------------------------------------------------------
Tick == /\ now < MaxT /\ now' = now + 1
        /\ UNCHANGED <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags, topic,
                       inflight, bus, epoch, pbAlive, pbEpoch, pbSent, mergedSet, obs, badLaunch, scn>>

MergePR(l) == /\ l = "l1" /\ l \notin mergedSet /\ mergedSet' = mergedSet \cup {l}
              /\ UNCHANGED <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags, topic,
                             inflight, bus, epoch, pbAlive, pbEpoch, pbSent, now, obs, badLaunch, scn>>

\* A request reaches the topic: the scenario fixes the order, the arrival time is free.
Arrive == /\ Len(topic) < Len(scn)
          /\ topic' = Append(topic, scn[Len(topic) + 1])
          /\ UNCHANGED <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags,
                         inflight, bus, epoch, pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

\* ---- the broker -------------------------------------------------------------
Deliver(m) ==
  /\ m \in inflight
  /\ inflight' = inflight \ {m}
  /\ bus' = IF ~Fenced \/ m.ep = epoch THEN Append(bus, m) ELSE bus
  /\ UNCHANGED <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags, topic, epoch,
                 pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

\* ---- the publisher pa -------------------------------------------------------
Crash == /\ paAlive /\ crashes < MaxCrash
         /\ paAlive' = FALSE /\ pc' = "idle" /\ mem' = NONE /\ memOff' = 0
         /\ crashes' = crashes + 1
         /\ UNCHANGED <<S, paEpoch, remoteTags, topic, inflight, bus, epoch, pbAlive, pbEpoch,
                        pbSent, mergedSet, now, obs, badLaunch, scn>>

Boot == /\ ~paAlive
        /\ paAlive' = TRUE /\ epoch' = epoch + 1 /\ paEpoch' = epoch + 1
        /\ UNCHANGED <<S, mem, memOff, pc, crashes, remoteTags, topic, inflight, bus,
                       pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

\* Step 1: a pending generation is finished first; otherwise the pass fetches.
Start == /\ paAlive /\ pc = "idle"
         /\ IF S.pending # NONE /\ ResumeExact
              THEN /\ pc' = "written" /\ mem' = S.pending
              ELSE /\ pc' = "fetched" /\ mem' = NONE
         /\ UNCHANGED <<S, memOff, paAlive, paEpoch, crashes, remoteTags, topic, inflight, bus,
                        epoch, pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

Fetch == /\ paAlive /\ pc = "fetched" /\ pc' = "read"                       \* step 2
         /\ UNCHANGED <<S, mem, memOff, paAlive, paEpoch, crashes, remoteTags, topic, inflight,
                        bus, epoch, pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

ReadTopic == /\ paAlive /\ pc = "read" /\ pc' = "computed" /\ memOff' = Len(topic)   \* step 3
             /\ UNCHANGED <<S, mem, paAlive, paEpoch, crashes, remoteTags, topic, inflight, bus,
                            epoch, pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

\* Step 4. A pass with nothing to say ends. A recompute after a resume keeps the pending
\* generation number (hw is already raised).
ComputeStep ==
  /\ paAlive /\ pc = "computed" /\ mem = NONE
  /\ LET gen == IF S.pending # NONE THEN S.pending.gen ELSE S.hw + 1
         res == Compute(S.core, SubSeq(topic, S.core.off + 1, memOff), memOff, now, mergedSet, gen)
     IN /\ gen <= MaxGen
        /\ IF res.changed THEN mem' = res.p /\ pc' = "computed"
           ELSE mem' = NONE /\ pc' = "idle"
  /\ UNCHANGED <<S, memOff, paAlive, paEpoch, crashes, remoteTags, topic, inflight, bus, epoch,
                 pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

WritePending == /\ paAlive /\ pc = "computed" /\ mem # NONE                 \* step 5
                /\ S' = [S EXCEPT !.hw = mem.gen, !.pending = mem]
                /\ pc' = "written"
                /\ UNCHANGED <<mem, memOff, paAlive, paEpoch, crashes, remoteTags, topic, inflight,
                               bus, epoch, pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

PushTags == /\ paAlive /\ pc = "written"                                     \* step 6
            /\ remoteTags' = IF TagsFirst THEN remoteTags \cup {mem.rec.tag} ELSE remoteTags
            /\ pc' = "tagged"
            /\ UNCHANGED <<S, mem, memOff, paAlive, paEpoch, crashes, topic, inflight, bus, epoch,
                           pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

Send == /\ paAlive /\ pc = "tagged"                                          \* step 7
        /\ inflight' = inflight \cup {[rec |-> mem.rec, ep |-> paEpoch]}
        /\ pc' = "sent"
        /\ UNCHANGED <<S, mem, memOff, paAlive, paEpoch, crashes, remoteTags, topic, bus, epoch,
                       pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

Ack == /\ paAlive /\ pc = "sent"
       /\ \/ /\ \E i \in DOMAIN bus : bus[i].rec = mem.rec /\ bus[i].ep = paEpoch
             /\ pc' = "published" /\ UNCHANGED <<paAlive, mem>>
          \/ /\ Fenced /\ paEpoch # epoch                \* fenced: the publish fails, the process ends
             /\ paAlive' = FALSE /\ pc' = "idle" /\ mem' = NONE
       /\ UNCHANGED <<S, memOff, paEpoch, crashes, remoteTags, topic, inflight, bus, epoch,
                      pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

Commit == /\ paAlive /\ pc = "published"                                     \* step 8
          /\ S' = [S EXCEPT !.core = mem.core, !.pending = NONE]
          /\ remoteTags' = IF TagsFirst THEN remoteTags ELSE remoteTags \cup {mem.rec.tag}
          /\ pc' = "idle" /\ mem' = NONE
          /\ UNCHANGED <<memOff, paAlive, paEpoch, crashes, topic, inflight, bus, epoch,
                         pbAlive, pbEpoch, pbSent, mergedSet, now, obs, badLaunch, scn>>

\* ---- the second publisher pb on another host -----------------------------
BootB == /\ ~pbAlive /\ pbAlive' = TRUE /\ epoch' = epoch + 1 /\ pbEpoch' = epoch + 1
         /\ UNCHANGED <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags, topic, inflight,
                        bus, pbSent, mergedSet, now, obs, badLaunch, scn>>

SendB(g) == /\ pbAlive /\ ~pbSent /\ pbSent' = TRUE
            /\ inflight' = inflight \cup {[rec |-> [gen |-> g, pub |-> "h2", live |-> {}, rel |-> NoRel,
                                                     vu |-> INF, tag |-> g], ep |-> pbEpoch]}
            /\ UNCHANGED <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags, topic, bus,
                           epoch, pbAlive, pbEpoch, mergedSet, now, obs, badLaunch, scn>>

\* ---- readers and placement --------------------------------------------------
IsAcc(b) == ~ReaderFilters \/ b.rec.pub = "h1"
AccSeq == SelectSeq(bus, IsAcc)
HeldRec == CHOOSE r \in {AccSeq[i].rec : i \in DOMAIN AccSeq} :
             \A x \in {AccSeq[i].rec : i \in DOMAIN AccSeq} : x.gen <= r.gen

Observe(h) == /\ AccSeq # <<>> /\ obs' = [obs EXCEPT ![h] = HeldRec.gen]
              /\ UNCHANGED <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags, topic,
                             inflight, bus, epoch, pbAlive, pbEpoch, pbSent, mergedSet, now, badLaunch, scn>>

\* Placement admits a host that answers the held generation, while the held record's
\* valid_until is ahead, however old the record is (the publisher may be stopped).
Launch(h) == /\ AccSeq # <<>> /\ obs[h] = HeldRec.gen
             /\ (CheckValidUntil => now < HeldRec.vu)
             /\ badLaunch' = (badLaunch \/ ~(now < HeldRec.vu /\ obs[h] = HeldRec.gen))
             /\ UNCHANGED <<S, mem, memOff, pc, paAlive, paEpoch, crashes, remoteTags, topic,
                            inflight, bus, epoch, pbAlive, pbEpoch, pbSent, mergedSet, now, obs, scn>>

Next == Tick \/ Crash \/ Boot \/ Start \/ Fetch \/ ReadTopic \/ ComputeStep \/ WritePending
        \/ PushTags \/ Send \/ Ack \/ Commit \/ BootB
        \/ (\E l \in Layers : MergePR(l)) \/ Arrive
        \/ (\E m \in inflight : Deliver(m)) \/ (\E g \in {1} : SendB(g))
        \/ (\E h \in Hosts : Observe(h)) \/ (\E h \in Hosts : Launch(h))

Spec == Init /\ [][Next]_vars

\* ---- invariants -------------------------------------------------------------
TypeOK ==
  /\ S.hw \in 0..MaxGen /\ pc \in {"idle", "fetched", "read", "computed", "written", "tagged", "sent", "published"}
  /\ remoteTags \subseteq 1..MaxGen

AccRecs == [i \in DOMAIN AccSeq |-> AccSeq[i].rec]

GenerationMonotone ==
  /\ \A i, j \in DOMAIN AccRecs : i < j => AccRecs[i].gen <= AccRecs[j].gen
  /\ S.pending # NONE => S.hw >= S.pending.gen

OnePublisherAccepted == \A i \in DOMAIN AccRecs : AccRecs[i].pub = "h1"

PendingTagsBeforePublish ==
  \A i \in DOMAIN bus : bus[i].rec.pub = "h1" => bus[i].rec.tag \in remoteTags

PendingRepublishedExactly ==
  \A i, j \in DOMAIN bus : (bus[i].rec.pub = "h1" /\ bus[j].rec.pub = "h1"
                            /\ bus[i].rec.gen = bus[j].rec.gen) => bus[i].rec = bus[j].rec

ReleasedLayerNeverReturns ==
  \A i, j \in DOMAIN AccRecs : i < j =>
    \A l \in Layers : AccRecs[i].rel[l] # "none" => l \notin AccRecs[j].live

DependentLeavesWithDependency ==
  \A i \in DOMAIN AccRecs : \A x \in AccRecs[i].live :
    Dep[x] # "none" => AccRecs[i].rel[Dep[x]] \in {"none", "merged"}

StaleRequestNeverApplies ==
  /\ \A a \in S.core.applied : a.named = a.at
  /\ (S.pending # NONE => \A b \in S.pending.core.applied : b.named = b.at)

NoLaunchOnExpiredLayer == ~badLaunch      \* set when a lane was admitted past valid_until or on a stale host
=============================================================================
