---------------------------- MODULE LanderLease ----------------------------
(***************************************************************************)
(* The lane lander (OMN-22186, task L1 of the lane lander plan): the       *)
(* permit, the fence and the kept tree, one pull request, one kept tree,   *)
(* one permit (one dispatch_id).                                           *)
(*                                                                         *)
(* The controller grants a lease on a kept tree, issues a permit stamped   *)
(* with issued_at on its own clock, sends offer / cancel / status /        *)
(* release, ends the permit when status shows it terminal or at            *)
(* cutoff_at = issued_at + HoldS, and then falls back to the cold worker.  *)
(* The host job runs offer, START, engine, push, done; it may die between  *)
(* any two steps and status then finishes it (done(failed) or, if it had   *)
(* reached pushing, done(unresolved)); nothing is replayed. Messages may   *)
(* be lost, late (they stay in the network set until delivered) or         *)
(* reordered (the set is unordered); the host may pause (its steps         *)
(* interleave freely with the controller's ticks). The only absolute time  *)
(* is the controller's tick count now; cutoff_at is a fixed function of    *)
(* issued_at. The host has no clock.                                       *)
(*                                                                         *)
(* Action names are the node transition names; the README has the table.   *)
(*                                                                         *)
(* Left out, on purpose: the lane unit and the engine scope drain (the     *)
(* tree state fenced), the WIP push, the amended-head refusal (every       *)
(* commit here descends from the base), the controller budget and ladder,  *)
(* more than one pull request or permit per tree.                          *)
(*                                                                         *)
(* Each design knob below is one decision of the plan; every mutant        *)
(* config flips exactly one of them and must violate the property it       *)
(* protects.                                                               *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    HoldS,               \* hold_s in ticks: cutoff_at = issued_at + HoldS
    Horizon,             \* last controller tick the model explores
    MaxOffers,           \* offers the controller sends for the one permit
    PermitLease,         \* TRUE: the fenced push is --force-with-lease on expected_head
    ColdLease,           \* TRUE: the cold worker's push leases on the head it started from
    ColdGuard,           \* "permit_ended" | "head_only": what a cold dispatch waits for
    OfferRule,           \* "find_record" | "respawn": a repeated offer finds the record or reruns
    RemoverRule,         \* "obey_record" | "ignore_record": a remover skips a tree a record names
    LockPlace,           \* "outside" | "inside": the lock file is outside or inside the tree
    FallbackAfterCutoff  \* FALSE suppresses every fallback decision once cutoff_at has passed

Base       == 0          \* the red head the permit and the cold worker both start from
PermitHead == 1          \* the commit the permit's engine makes
ColdHead   == 2          \* the commit the cold worker makes (divergent from PermitHead)
Heads      == 0..2
Msgs       == {"offer", "cancel", "stq", "ansT", "ansF", "rel"}
Live       == {"offered", "started", "engine", "committed", "pushing"}

VARIABLES
    now,          \* the controller's tick: the only absolute time
    lease,        \* "none" | "granted"
    perm,         \* the controller's view of the permit: "none" | "live" | "ended"
    issued,       \* issued_at, stamped once on the controller's clock
    cutoff,       \* cutoff_at = issued_at + HoldS
    obsTerm,      \* status has shown the permit terminal
    seenHead,     \* the head the controller last read from the watcher
    decision,     \* the fallback decision: "none" | "gate" | "escalate" | "cold"
    cold,         \* the cold worker: "none" | "dispatched" | "pushed" | "rejected"
    coldBase,     \* the head the cold worker started from (its lease)
    coldWasLive,  \* history: the dispatch fell inside a live, unobserved-terminal permit
    merged,       \* the controller merged the verified head
    offers,       \* offers sent for the permit
    net,          \* messages in the network
    hp,           \* host permit record: "none" | "offered" | "started" | "engine" | "committed" | "pushing" | "done"
    hres,         \* the done result: "none" | "pushed" | "nochange" | "failed" | "unresolved" | "declined" | "cancelled"
    job,          \* the host job process: "none" | "alive" | "dead"
    engines,      \* engine children spawned for the one dispatch_id
    tomb,         \* a cancel tombstone exists
    pdone,        \* the fenced push has run (its record is still pushing)
    pres,         \* the fenced push result: "none" | "ok" | "rejected"
    remote,       \* the PR branch head on the remote
    landed,       \* every commit a push has ever been accepted for
    tree,         \* "kept" | "removed"
    rec,          \* the tree record: "ready" | "none"
    lockOwner,    \* the tree lock grant holder: "none" | "P" | "R" | "X"
    rst,          \* the release job: "idle" | "locked" | "closed" | "done"
    delBy,        \* who deleted the tree
    delRec        \* a record named the tree when it was deleted

vars == << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold, coldBase,
           coldWasLive, merged, offers, net, hp, hres, job, engines, tomb, pdone, pres,
           remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

NeedsFix   == remote = Base /\ ~merged
LiveWindow == perm # "none" /\ issued <= now /\ now < cutoff /\ ~obsTerm
Stranded   == perm = "ended" /\ NeedsFix /\ now >= cutoff /\ decision = "none"
Duty       == (perm = "live" /\ now >= cutoff) \/ Stranded
AllowedNow == FallbackAfterCutoff \/ perm = "none" \/ now < cutoff

TypeOK ==
    /\ now \in 0..Horizon
    /\ lease \in {"none", "granted"}
    /\ perm \in {"none", "live", "ended"}
    /\ issued \in 0..Horizon /\ cutoff \in 0..(Horizon + HoldS)
    /\ obsTerm \in BOOLEAN /\ coldWasLive \in BOOLEAN /\ merged \in BOOLEAN
    /\ seenHead \in Heads /\ coldBase \in Heads /\ remote \in Heads /\ landed \subseteq Heads
    /\ decision \in {"none", "gate", "escalate", "cold"}
    /\ cold \in {"none", "dispatched", "pushed", "rejected"}
    /\ offers \in 0..MaxOffers /\ net \subseteq Msgs
    /\ hp \in {"none", "done"} \cup Live
    /\ hres \in {"none", "pushed", "nochange", "failed", "unresolved", "declined", "cancelled"}
    /\ job \in {"none", "alive", "dead"}
    /\ engines \in 0..2 /\ tomb \in BOOLEAN /\ pdone \in BOOLEAN
    /\ pres \in {"none", "ok", "rejected"}
    /\ tree \in {"kept", "removed"} /\ rec \in {"ready", "none"}
    /\ lockOwner \in {"none", "P", "R", "X"}
    /\ rst \in {"idle", "locked", "closed", "done"}
    /\ delBy \in {"none", "R", "X"} /\ delRec \in BOOLEAN

Init ==
    /\ now = 0 /\ lease = "none" /\ perm = "none" /\ issued = 0 /\ cutoff = 0
    /\ obsTerm = FALSE /\ seenHead = Base /\ decision = "none"
    /\ cold = "none" /\ coldBase = Base /\ coldWasLive = FALSE
    /\ merged = FALSE /\ offers = 0 /\ net = {}
    /\ hp = "none" /\ hres = "none" /\ job = "none" /\ engines = 0 /\ tomb = FALSE
    /\ pdone = FALSE /\ pres = "none"
    /\ remote = Base /\ landed = {Base}
    /\ tree = "kept" /\ rec = "ready" /\ lockOwner = "none" /\ rst = "idle"
    /\ delBy = "none" /\ delRec = FALSE

(***************************** the controller *****************************)

\* A tick completes only when its decisions are made: the permit is ended at
\* cutoff_at and a stranded pull request has its fallback decision.
Tick ==
    /\ now < Horizon /\ ~Duty
    /\ now' = now + 1
    /\ UNCHANGED << lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

\* The request is the tree's STATUS row; status shows the record ready.
GrantLease ==
    /\ lease = "none" /\ tree = "kept" /\ rec = "ready" /\ ~merged
    /\ lease' = "granted"
    /\ UNCHANGED << now, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

\* Issue the permit and stamp issued_at once; cutoff_at is a function of it.
IssuePermit ==
    /\ lease = "granted" /\ perm = "none" /\ cold = "none" /\ NeedsFix
    /\ now + HoldS <= Horizon
    /\ perm' = "live" /\ issued' = now /\ cutoff' = now + HoldS
    /\ obsTerm' = FALSE /\ offers' = 0
    /\ UNCHANGED << now, lease, seenHead, decision, cold, coldBase, coldWasLive, merged,
                 net, hp, hres, job, engines, tomb, pdone, pres, remote, landed, tree,
                 rec, lockOwner, rst, delBy, delRec >>

SendOffer ==
    /\ perm = "live" /\ offers < MaxOffers
    /\ net' = net \cup {"offer"} /\ offers' = offers + 1
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, hp, hres, job, engines, tomb, pdone,
                 pres, remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

\* The controller sends cancel each tick until status shows the permit terminal.
SendCancel ==
    /\ perm \in {"live", "ended"} /\ ~obsTerm
    /\ net' = net \cup {"cancel"}
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

ReadStatus ==
    /\ perm \in {"live", "ended"} /\ ~obsTerm
    /\ net' = net \cup {"stq"}
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

\* An answer may come late, out of order, or never; only a terminal one counts.
RecvStatus ==
    \E m \in {"ansT", "ansF"} :
        /\ m \in net
        /\ net' = net \ {m}
        /\ obsTerm' = (obsTerm \/ m = "ansT")
        /\ UNCHANGED << now, lease, perm, issued, cutoff, seenHead, decision, cold, coldBase,
                 coldWasLive, merged, offers, hp, hres, job, engines, tomb, pdone,
                 pres, remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

\* The permit's lease row ends when status shows it terminal or at cutoff_at.
EndPermit ==
    /\ perm = "live" /\ (obsTerm \/ now >= cutoff)
    /\ perm' = "ended"
    /\ UNCHANGED << now, lease, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

ObserveHead ==
    /\ seenHead # remote
    /\ seenHead' = remote
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, decision, cold, coldBase,
                 coldWasLive, merged, offers, net, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

Merge ==
    /\ remote # Base /\ seenHead = remote /\ ~merged
    /\ merged' = TRUE
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, offers, net, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

\* The release request: merged or closed, ttl_s, kept_grace_s or a HOLD.
SendRelease ==
    /\ lease = "granted"
    /\ net' = net \cup {"rel"}
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

ColdPermitOk == IF ColdGuard = "permit_ended" THEN perm # "live" ELSE TRUE

\* The cold worker, the fallback: it reads an unchanged head AND waits for the
\* permit to be ended.
ColdDispatch ==
    /\ cold = "none" /\ decision \in {"none", "gate"}
    /\ ColdPermitOk /\ seenHead = Base /\ ~merged /\ AllowedNow
    /\ cold' = "dispatched" /\ coldBase' = seenHead
    /\ coldWasLive' = LiveWindow /\ decision' = "cold"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, merged, offers,
                 net, hp, hres, job, engines, tomb, pdone, pres, remote, landed, tree,
                 rec, lockOwner, rst, delBy, delRec >>

\* A named worker gate holds the dispatch this tick, or the head-cap escalates.
WorkerGate ==
    /\ Stranded /\ AllowedNow
    /\ decision' = "gate"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, cold, coldBase,
                 coldWasLive, merged, offers, net, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

HeadCapEscalate ==
    /\ Stranded /\ AllowedNow
    /\ decision' = "escalate"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, cold, coldBase,
                 coldWasLive, merged, offers, net, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

\* The decision a stranded pull request is owed on the tick.
Decide == Stranded /\ (ColdDispatch \/ WorkerGate \/ HeadCapEscalate)

(************************ the cold worker's lease push *********************)

ColdPushLands ==
    /\ cold = "dispatched" /\ (~ColdLease \/ remote = coldBase)
    /\ remote' = ColdHead /\ landed' = landed \cup {ColdHead} /\ cold' = "pushed"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, pdone, pres, tree, rec, lockOwner, rst, delBy, delRec >>

ColdPushRefused ==
    /\ cold = "dispatched" /\ ColdLease /\ remote # coldBase
    /\ cold' = "rejected"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

(****************************** the host job ******************************)

OfferNew ==
    /\ "offer" \in net /\ hp = "none" /\ ~tomb
    /\ net' = net \ {"offer"} /\ hp' = "offered" /\ job' = "alive"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, hres, engines, tomb, pdone,
                 pres, remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

OfferCancelled ==
    /\ "offer" \in net /\ hp = "none" /\ tomb
    /\ net' = net \ {"offer"} /\ hp' = "done" /\ hres' = "cancelled"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, job, engines, tomb, pdone,
                 pres, remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

\* A repeated offer finds the record and starts nothing.
OfferRepeat ==
    /\ "offer" \in net /\ hp # "none"
    /\ (OfferRule = "find_record" \/ lockOwner = "P")
    /\ net' = net \ {"offer"}
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

\* Mutant: a repeated offer reruns the job, whatever the record says.
OfferRespawn ==
    /\ OfferRule = "respawn"
    /\ "offer" \in net /\ hp # "none" /\ lockOwner # "P"
    /\ net' = net \ {"offer"} /\ hp' = "offered" /\ hres' = "none" /\ job' = "alive"
    /\ pdone' = FALSE /\ pres' = "none"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, engines, tomb, remote, landed,
                 tree, rec, lockOwner, rst, delBy, delRec >>

HostCancel ==
    /\ "cancel" \in net
    /\ net' = net \ {"cancel"} /\ tomb' = TRUE
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, hp, hres, job, engines, pdone,
                 pres, remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

\* START: the lock without waiting, the tree kept, the head still expected_head.
HostStart ==
    /\ job = "alive" /\ hp = "offered" /\ ~tomb
    /\ lockOwner = "none" /\ tree = "kept" /\ rec = "ready" /\ remote = Base
    /\ hp' = "started" /\ lockOwner' = "P"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, rst, delBy, delRec >>

HostDecline ==
    /\ job = "alive" /\ hp = "offered" /\ ~tomb
    /\ ~(lockOwner = "none" /\ tree = "kept" /\ rec = "ready" /\ remote = Base)
    /\ hp' = "done" /\ hres' = "declined" /\ job' = "none"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, engines, tomb, pdone,
                 pres, remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

\* The run checks the tombstone before START, before the engine and before the push.
HostAbort ==
    /\ job = "alive" /\ tomb /\ hp \in {"offered", "started", "committed"}
    /\ hp' = "done" /\ hres' = "cancelled" /\ job' = "none"
    /\ lockOwner' = (IF lockOwner = "P" THEN "none" ELSE lockOwner)
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, engines, tomb, pdone,
                 pres, remote, landed, tree, rec, rst, delBy, delRec >>

HostEngine ==
    /\ job = "alive" /\ hp = "started" /\ ~tomb /\ engines < 2
    /\ hp' = "engine" /\ engines' = engines + 1
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hres, job, tomb, pdone,
                 pres, remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

HostEngineCommits ==
    /\ job = "alive" /\ hp = "engine"
    /\ hp' = "committed"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

HostEngineNoChange ==
    /\ job = "alive" /\ hp = "engine"
    /\ hp' = "done" /\ hres' = "nochange" /\ job' = "none"
    /\ lockOwner' = (IF lockOwner = "P" THEN "none" ELSE lockOwner)
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, engines, tomb, pdone,
                 pres, remote, landed, tree, rec, rst, delBy, delRec >>

\* Write pushing{expected_head, new_head}.
HostWritePushing ==
    /\ job = "alive" /\ hp = "committed" /\ ~tomb
    /\ hp' = "pushing"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, lockOwner, rst, delBy,
                 delRec >>

\* The fenced push: new_head to the branch, with the lease on expected_head.
HostPush ==
    /\ job = "alive" /\ hp = "pushing" /\ ~pdone
    /\ pdone' = TRUE
    /\ \/ /\ (~PermitLease \/ remote = Base)
          /\ remote' = PermitHead /\ landed' = landed \cup {PermitHead} /\ pres' = "ok"
       \/ /\ PermitLease /\ remote # Base
          /\ pres' = "rejected" /\ UNCHANGED << remote, landed >>
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, tree, rec, lockOwner, rst, delBy, delRec >>

HostDone ==
    /\ job = "alive" /\ hp = "pushing" /\ pdone
    /\ hp' = "done" /\ hres' = (IF pres = "ok" THEN "pushed" ELSE "failed") /\ job' = "none"
    /\ lockOwner' = (IF lockOwner = "P" THEN "none" ELSE lockOwner)
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, engines, tomb, pdone,
                 pres, remote, landed, tree, rec, rst, delBy, delRec >>

\* The job may die between any two steps; the operating system drops its lock.
JobDies ==
    /\ job = "alive" /\ hp \in Live
    /\ job' = "dead"
    /\ lockOwner' = (IF lockOwner = "P" THEN "none" ELSE lockOwner)
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, rst, delBy, delRec >>

\* status finishes a dead job by the fail-closed rule and answers; nothing is replayed.
HostStatus ==
    /\ "stq" \in net
    /\ LET fin == (job = "dead" /\ hp \in Live)
           hp2 == IF fin THEN "done" ELSE hp
       IN /\ hp' = hp2
          /\ hres' = (IF fin THEN (IF hp = "pushing" THEN "unresolved" ELSE "failed") ELSE hres)
          /\ job' = (IF fin THEN "none" ELSE job)
          /\ net' = (net \ {"stq"}) \cup {IF hp2 = "done" THEN "ansT" ELSE "ansF"}
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, engines, tomb, pdone, pres,
                 remote, landed, tree, rec, lockOwner, rst, delBy, delRec >>

(************************ the kept tree: lock and remove *******************)

\* The release job: the lock without waiting, closeout under its grant, the record last.
ReleaseLock ==
    /\ "rel" \in net /\ rst = "idle" /\ tree = "kept" /\ rec = "ready" /\ lockOwner = "none"
    /\ net' = net \ {"rel"} /\ lockOwner' = "R" /\ rst' = "locked"
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, hp, hres, job, engines, tomb,
                 pdone, pres, remote, landed, tree, rec, delBy, delRec >>

ReleaseCloseout ==
    /\ rst = "locked"
    /\ tree' = "removed" /\ delBy' = "R" /\ delRec' = (rec # "none") /\ rst' = "closed"
    /\ lockOwner' = (IF LockPlace = "inside" THEN "none" ELSE lockOwner)
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, pdone, pres, remote, landed, rec >>

ReleaseRecord ==
    /\ rst = "closed"
    /\ rec' = "none" /\ rst' = "done"
    /\ lockOwner' = (IF lockOwner = "R" THEN "none" ELSE lockOwner)
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, pdone, pres, remote, landed, tree, delBy, delRec >>

\* Any other remover or writer: the lock without waiting, skip a tree a record names.
RemoverRuns ==
    /\ tree = "kept" /\ lockOwner = "none"
    /\ (RemoverRule = "ignore_record" \/ rec = "none")
    /\ tree' = "removed" /\ delBy' = "X" /\ delRec' = (rec # "none")
    /\ UNCHANGED << now, lease, perm, issued, cutoff, obsTerm, seenHead, decision, cold,
                 coldBase, coldWasLive, merged, offers, net, hp, hres, job, engines,
                 tomb, pdone, pres, remote, landed, rec, lockOwner, rst >>

Next ==
    \/ Tick \/ GrantLease \/ IssuePermit \/ SendOffer \/ SendCancel \/ ReadStatus
    \/ RecvStatus \/ EndPermit \/ ObserveHead \/ Merge \/ SendRelease
    \/ ColdDispatch \/ WorkerGate \/ HeadCapEscalate
    \/ ColdPushLands \/ ColdPushRefused
    \/ OfferNew \/ OfferCancelled \/ OfferRepeat \/ OfferRespawn \/ HostCancel
    \/ HostStart \/ HostDecline \/ HostAbort \/ HostEngine \/ HostEngineCommits
    \/ HostEngineNoChange \/ HostWritePushing \/ HostPush \/ HostDone \/ JobDies
    \/ HostStatus
    \/ ReleaseLock \/ ReleaseCloseout \/ ReleaseRecord \/ RemoverRuns

\* Fair ticks: the tick completes its decisions (the permit ends at cutoff_at,
\* a stranded pull request is decided) and the next tick comes.
Spec == Init /\ [][Next]_vars /\ WF_vars(Tick) /\ WF_vars(EndPermit) /\ WF_vars(Decide)

(******************************** properties ********************************)

\* No cold dispatch inside [issued_at, cutoff_at) while the permit is not observed terminal.
AtMostOneAuthorizedPublisher == ~coldWasLive

\* Every accepted commit stays reachable from the branch head; no two divergent heads both land.
NoLostUpdate == landed \ {Base} \subseteq {remote}

\* One dispatch_id, at most one engine child.
ExactlyOnceChild == engines <= 1

\* Only the lock holder mutates a kept tree; only a release job holding the grant
\* deletes a tree that a record names.
SingleOwnerOfTree ==
    /\ (job = "alive" /\ hp \in {"started", "engine", "committed", "pushing"}) => lockOwner = "P"
    /\ rst \in {"locked", "closed"} => lockOwner = "R"
    /\ delRec => delBy = "R"

\* An ended or expired permit on a pull request that still needs a fix is followed by a
\* cold dispatch, a named worker gate or the head-cap escalation, unless the head moved.
NoStrand == [](Stranded => <>(decision # "none" \/ ~NeedsFix))
=============================================================================
