--------------------------- MODULE HostConverge -----------------------------
(* One joined host, two reconciler processes (ll and the five-minute one-shot os),
   the reconciler's republish task, the runner, and two lanes a and b. os starts
   only while ll is down; ll may boot and take over while os has a pass in flight.
   There is no global pass owner. Each process has its own pass program counter.

   Takeover takes the role lock, opens a newer producer epoch, then recovers the
   host sequence from the newest records of the reconciler role on the bus. The
   role lock protects takeover, not an entire process lifetime. Opening an epoch
   fences ALL older producers of that role. Their next publish fails and ends
   the process. The host fence (a set of holders, released on death) serialises
   physical mutations. The separate state lock protects state-file RMWs; the
   republish task does not need the physical mutation fence.

   A surface visit takes the fence, reads the newest desired generation, installs,
   reads back, reads/writes the pass verdict under the state lock, then releases
   the fence. Publication is later and copies the readback record unchanged.
   Unguarded manifest writes can occur between install and readback, and between
   passes. A mismatch produces FAILED; the next visit restores the manifest.
   Each pass visits a surface at most once, oldest wait first, and can end after
   one visit at its budget. Ending behind starts the next pass immediately.

   Invariants and their single-constant mutants:
     NoObservedBeforeReadback: every published CONVERGED record has rbd=TRUE,
       set ONLY by Readback. PublishAfterReadback=FALSE publishes at Install.
     NoLaneOnUnverifiedTree: history of admission never includes an absent or
       tampered tree. RecheckUnderFence=FALSE omits the runner's final check.
     PinnedTreeNeverRemoved: history of removal never includes a tree named by
       a pin AT REMOVAL. CollectRespectsPins=FALSE permits that removal.
     OrphanPinReleased: no sweep retains a dead lane's pin, including a pid
       reused within the same boot. PinChecksStart=FALSE checks only the pid.
     OwnedSurfaceOneWriter: wrote intersect KNOWN writers is a subset of
       {reconciler}. UnguardedFixOn=TRUE enables the host-drift fix's unguarded
       claude plugin write. Holds while all such known unguarded actions are off.
       OwnerGuard=TRUE refuses hand install-tree, its separate hot-patch write,
       the fix's install-plugin-tree, and adopt-merged's plugin target.
     ForeignWriteReported: no published CONVERGED readback saw foreign manifest
       contents. ReadbackCompares=FALSE suppresses FAILED after a real mismatch.
       UnknownWriterOn=TRUE in the design: unknown is NOT a known writer and
       is covered by this invariant, not OwnedSurfaceOneWriter.
     TakeoverBeforeMutation: a history flag covers tree build, manifest install,
       sweep, collection, pass verdict writes AND republish state writes before
       takeover completes. TakeoverFirst=FALSE allows a booted process to work.
     StateWritesSerialised: state-file pass/republish write counters equal the
       committed RMW counts. StateLock=FALSE admits a lost update. Republish
       snapshots both the surface record and next host sequence under that lock.
     EverySurfaceWithinTwoPasses: neither surface misses two completed passes
       of a process. OrderByWait=FALSE always puts T first, starving I at budget.

   Limits: this is a bounded scenario suite, NOT arbitrary workload exploration.
   Script blocks are partial orders: all enabled events in a block interleave;
   singleton blocks fix irrelevant order. In particular neither process can
   acquire a held host fence, even in the overlapping-pass block. Explicit
   scenarios cover both sides of the install/readback race, foreign writes
   between passes, state RMW overlap, epoch takeover during a pass, a desired
   update mid-pass, budget starvation, collection before/after admission, two
   simultaneous pins on the same/different trees, pid reuse, and death after
   each surface step (including death holding the state lock). MaxCrash bounds
   reconciler/runner-admission deaths together; lane deaths are one per lane.
   Two generations, two surfaces, one boot id, one pid/start per lane plus one
   reused start, no real clock (age is an event), no tree bytes or real hashes.
   Tree corruption is an environment event representing an unknown writer.
   The host is already owned: guarded writers always record refusals when
   OwnerGuard=TRUE. Unknown writes deliberately bypass ownership and the fence.
   Bus records are retained as a finite history; no transport retries, timing
   fairness, throughput claim, or proof beyond these partial-order scenarios.
   Every script is finite and all numeric growth is bounded by its events. *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS MaxCrash, OwnerGuard,
          RecheckUnderFence, PublishAfterReadback, CollectRespectsPins,
          PinChecksStart, UnguardedFixOn, UnknownWriterOn, ReadbackCompares,
          TakeoverFirst, StateLock, OrderByWait

Procs == {"ll", "os"}
Surf == {"T", "I"}
Trees == {"t1", "t2"}
Lanes == {"a", "b"}
Known == {"reconciler", "hand", "fixg", "adopt", "fixu"}
W(a, task) == <<a, task>>
NoHolder == <<"none", "none">>
Writers == {W(a, task) : a \in Procs, task \in {"pass", "republish"}}
TreeOf(g) == IF g = 1 THEN "t1" ELSE "t2"
Max(n, m) == IF n > m THEN n ELSE m
Min2(n) == IF n > 2 THEN 2 ELSE n
NoRec == [status |-> "NONE", tree |-> "none", gen |-> 0,
          rbd |-> FALSE, fgn |-> FALSE]
Rec(x, g, good, evidence, foreign) ==
  [status |-> IF good THEN "CONVERGED" ELSE "FAILED",
   tree |-> IF x = "T" THEN TreeOf(g) ELSE "none", gen |-> g,
   rbd |-> evidence, fgn |-> foreign]
BusRec(epoch, seq, x, rec) ==
  [role |-> "reconciler", epoch |-> epoch, seq |-> seq, surface |-> x, rec |-> rec]
Sf0 == [m |-> 0, r |-> 0, seq |-> 0, records |-> [x \in Surf |-> NoRec]]
Pin0 == [exists |-> FALSE, tree |-> "none", start |-> 0, boot |-> 1]

\* Event arguments identify a process/lane and, when needed, a surface or tree.
E(op, a, x) == [op |-> op, a |-> a, x |-> x]
One(op, a, x) == <<{E(op, a, x)}>>
Ready(a) == One("boot", a, "") \o One("role", a, "")
            \o One("epoch", a, "") \o One("recover", a, "")
Visit(a) == One("take", a, "") \o One("install", a, "")
            \o One("readback", a, "") \o One("pread", a, "")
            \o One("pwrite", a, "") \o One("release", a, "")
FullPass(a) == One("begin", a, "") \o Visit(a) \o One("continue", a, "")
               \o Visit(a) \o One("end", a, "")
Republish(a, x) == One("rread", a, x) \o One("rwrite", a, "")
                   \o One("publish", a, "")
TreeReady == Ready("ll") \o FullPass("ll") \o Republish("ll", "T")
Lane(l) == One("plan", l, "") \o One("admit", l, "")
           \o One("launch", l, "")
Maintenance(op, t) == One("mtake", "ll", "") \o One(op, "ll", t)
                      \o One("mrelease", "ll", "")
FinishVisit(a) == One("pread", a, "") \o One("pwrite", a, "")
                  \o One("release", a, "")

Cases == {<<n, 0>> : n \in {"budget", "mid-pass", "foreign-window", "foreign-between",
          "state-race", "overlap", "takeover-window", "pins-same",
          "pins-different", "admission-collect", "admission-tamper", "orphan",
          "runner-crash"}} \cup
          {<<kind, n>> : kind \in {"crash-T", "crash-I"}, n \in 1..6}
Script(c) ==
  CASE c[1] = "budget" ->
         Ready("ll") \o One("begin", "ll", "") \o Visit("ll")
         \o One("generation", "", "") \o One("end", "ll", "")
         \o Visit("ll") \o One("end", "ll", "")
         \o Visit("ll") \o One("end", "ll", "")
         \o Republish("ll", "T") \o Republish("ll", "I")
    [] c[1] = "mid-pass" ->
         Ready("ll") \o One("begin", "ll", "") \o Visit("ll")
         \o One("generation", "", "") \o One("continue", "ll", "")
         \o Visit("ll") \o One("end", "ll", "")
         \o Visit("ll") \o One("end", "ll", "")
         \o Republish("ll", "T") \o Republish("ll", "I")
    [] c[1] = "foreign-window" ->
         Ready("ll") \o One("begin", "ll", "") \o Visit("ll")
         \o One("continue", "ll", "") \o One("take", "ll", "")
         \o One("install", "ll", "")
         \o One("hand-install", "", "") \o One("hand-patch", "", "")
         \o One("fix-guarded", "", "") \o One("adopt", "", "")
         \o <<{E("fix-unguarded", "", ""), E("unknown", "", ""),
                E("readback", "ll", "")}>>
         \o FinishVisit("ll") \o One("end", "ll", "")
         \o Republish("ll", "I") \o FullPass("ll") \o Republish("ll", "I")
         \o FullPass("ll") \o Republish("ll", "I")
    [] c[1] = "foreign-between" ->
         Ready("ll") \o FullPass("ll") \o Republish("ll", "I")
         \o One("fix-unguarded", "", "") \o One("unknown", "", "")
         \o FullPass("ll")
         \o Republish("ll", "I") \o FullPass("ll") \o Republish("ll", "I")
    [] c[1] = "state-race" ->
         Ready("ll") \o FullPass("ll") \o One("begin", "ll", "")
         \o One("take", "ll", "") \o One("install", "ll", "")
         \o One("readback", "ll", "")
         \o <<{E("pread", "ll", ""), E("pwrite", "ll", ""),
                E("rread", "ll", "I"), E("rwrite", "ll", "")}>>
         \o One("release", "ll", "") \o One("publish", "ll", "")
    [] c[1] = "overlap" ->
         Ready("os") \o FullPass("os") \o Republish("os", "T")
         \o One("begin", "os", "") \o One("take", "os", "")
         \o Ready("ll") \o One("begin", "ll", "")
         \o <<{E("install", "os", ""), E("readback", "os", ""),
                E("pread", "os", ""), E("pwrite", "os", ""),
                E("release", "os", ""), E("take", "ll", ""),
                E("install", "ll", ""), E("readback", "ll", ""),
                E("pread", "ll", ""), E("pwrite", "ll", ""),
                E("release", "ll", "")}>>
         \o <<{E("continue", "os", ""), E("take", "os", ""),
                E("install", "os", ""), E("readback", "os", ""),
                E("pread", "os", ""), E("pwrite", "os", ""),
                E("release", "os", ""), E("continue", "ll", ""),
                E("take", "ll", ""), E("install", "ll", ""),
                E("readback", "ll", ""), E("pread", "ll", ""),
                E("pwrite", "ll", ""), E("release", "ll", "")}>>
         \o One("end", "os", "") \o One("end", "ll", "")
         \o Republish("os", "T") \o Republish("ll", "I")
    [] c[1] = "takeover-window" ->
         One("boot", "ll", "")
         \o <<{E("role", "ll", ""), E("epoch", "ll", ""),
                E("recover", "ll", ""), E("begin", "ll", ""),
                E("take", "ll", ""), E("install", "ll", ""),
                E("readback", "ll", ""), E("pread", "ll", ""),
                E("pwrite", "ll", ""), E("release", "ll", "")}>>
    [] c[1] = "pins-same" ->
         TreeReady \o Lane("a") \o Lane("b")
         \o One("generation", "", "") \o One("age", "", "t1")
         \o Maintenance("collect", "t1")
    [] c[1] = "pins-different" ->
         TreeReady \o Lane("a") \o One("generation", "", "")
         \o FullPass("ll") \o Republish("ll", "T") \o Lane("b")
         \o One("age", "", "t1") \o Maintenance("collect", "t1")
    [] c[1] = "admission-collect" ->
         TreeReady \o One("plan", "a", "")
         \o One("generation", "", "") \o One("age", "", "t1")
         \o Maintenance("collect", "t1")
         \o One("admit", "a", "") \o One("launch", "a", "")
    [] c[1] = "admission-tamper" ->
         TreeReady \o One("plan", "a", "") \o One("tamper", "", "t1")
         \o One("admit", "a", "") \o One("launch", "a", "")
    [] c[1] = "orphan" ->
         TreeReady \o Lane("a") \o Lane("b") \o One("lane-end", "a", "")
         \o One("lane-crash", "b", "") \o One("reuse", "b", "")
         \o Maintenance("sweep", "")
    [] c[1] = "runner-crash" ->
         TreeReady \o One("plan", "a", "") \o One("admit", "a", "")
         \o One("runner-crash", "a", "") \o Maintenance("sweep", "")
         \o Lane("b")
    [] OTHER ->
         Ready("ll") \o One("begin", "ll", "")
         \o (IF c[1] = "crash-I" THEN Visit("ll") \o One("continue", "ll", "")
             ELSE <<>>)
         \o SubSeq(Visit("ll"), 1, c[2]) \o One("process-crash", "ll", "")
         \o Ready("ll") \o FullPass("ll") \o Republish("ll", "T")

VARIABLES s, todo
vars == <<s, todo>>

Init ==
  /\ s = [dg |-> 1, rs |-> [a \in Procs |-> "down"],
          roleLock |-> "none", brokerEpoch |-> 0, epoch |-> [a \in Procs |-> 0],
          hostSeq |-> [a \in Procs |-> 0], publishFailed |-> {}, fence |-> {},
          pc |-> [a \in Procs |-> "idle"], pq |-> [a \in Procs |-> <<>>],
          done |-> [a \in Procs |-> {}], active |-> [a \in Procs |-> "T"],
          target |-> [a \in Procs |-> 1],
          since |-> [a \in Procs |-> [x \in Surf |-> 0]],
          maint |-> [a \in Procs |-> FALSE],
          trees |-> [t \in Trees |-> "absent"], tamper |-> [t \in Trees |-> FALSE],
          old |-> {}, instGen |-> 0, foreign |-> FALSE, failedI |-> FALSE,
          rb |-> [a \in Procs |-> NoRec], pub |-> [x \in Surf |-> NoRec],
          bus |-> {BusRec(0, 3, "I", NoRec)}, wrote |-> {}, refused |-> {},
          sf |-> Sf0, wpc |-> [w \in Writers |-> "idle"],
          wcopy |-> [w \in Writers |-> Sf0], lockHolder |-> NoHolder,
          rpc |-> [a \in Procs |-> "idle"], rx |-> [a \in Procs |-> "T"],
          reserve |-> [a \in Procs |-> 0], outgoing |-> [a \in Procs |-> NoRec],
          dm |-> 0, dr |-> 0, crashes |-> 0,
          lst |-> [l \in Lanes |-> "out"], plan |-> [l \in Lanes |-> "none"],
          pin |-> [l \in Lanes |-> Pin0], liveStart |-> [l \in Lanes |-> 0],
          badLaunch |-> FALSE, removedPinned |-> FALSE, orphanSurvived |-> FALSE,
          mutNoTake |-> FALSE]
  /\ \E c \in Cases : todo = Script(c)

Free == s.fence = {}
MutOk(a) == s.rs[a] = "up" \/ (~TakeoverFirst /\ s.rs[a] = "booted")
Noted(a) == s.mutNoTake \/ s.rs[a] # "up"
OwnFence(a) == s.fence = {a}
Order(wait) == IF OrderByWait /\ wait["I"] > wait["T"]
              THEN <<"I", "T">> ELSE <<"T", "I">>
Behind == s.trees[TreeOf(s.dg)] # "verified" \/ s.tamper[TreeOf(s.dg)]
          \/ s.instGen # s.dg \/ s.foreign \/ s.failedI
NewestSeq == CHOOSE n \in
               {b.seq : b \in {r \in s.bus : r.role = "reconciler"}} :
               \A b \in s.bus : b.role = "reconciler" => b.seq <= n
Stop(a) ==
  [s EXCEPT !.rs[a] = "down", !.pc[a] = "dead", !.maint[a] = FALSE,
            !.pq[a] = <<>>, !.done[a] = {}, !.rb[a] = NoRec,
            !.outgoing[a] = NoRec, !.reserve[a] = 0,
            !.wcopy[W(a, "pass")] = Sf0, !.wcopy[W(a, "republish")] = Sf0,
            !.fence = @ \ {a}, !.rpc[a] = "idle",
            !.roleLock = IF @ = a THEN "none" ELSE @,
            !.lockHolder = IF @ \in {W(a, "pass"), W(a, "republish")}
                           THEN NoHolder ELSE @,
            !.wpc[W(a, "pass")] = "idle", !.wpc[W(a, "republish")] = "idle"]

Boot(a) ==
  /\ s.rs[a] = "down"
  /\ a = "os" => s.rs["ll"] = "down"
  /\ s' = [s EXCEPT !.rs[a] = "booted", !.pc[a] = "idle",
                    !.epoch[a] = 0, !.hostSeq[a] = 0]
RoleLock(a) ==
  /\ s.rs[a] = "booted" /\ s.roleLock = "none"
  /\ s' = [s EXCEPT !.roleLock = a, !.rs[a] = "locked"]
OpenEpoch(a) ==
  /\ s.rs[a] = "locked" /\ s.roleLock = a
  /\ s' = [s EXCEPT !.brokerEpoch = @ + 1, !.epoch[a] = s.brokerEpoch + 1,
                    !.rs[a] = "opened"]
Recover(a) ==
  /\ s.rs[a] = "opened" /\ s.roleLock = a
  /\ s' = [s EXCEPT !.hostSeq[a] = NewestSeq, !.rs[a] = "up", !.roleLock = "none"]
ProcessCrash(a) ==
  /\ s.rs[a] # "down" /\ s.crashes < MaxCrash
  /\ s' = [Stop(a) EXCEPT !.crashes = @ + 1]

Begin(a) ==
  /\ MutOk(a) /\ s.pc[a] \in {"idle", "ready"} /\ s.done[a] = {}
  /\ s' = IF s.pc[a] = "ready" THEN s
          ELSE [s EXCEPT !.pc[a] = "ready", !.pq[a] = Order(s.since[a])]
Take(a) ==
  /\ MutOk(a) /\ s.pc[a] = "ready" /\ s.pq[a] # <<>> /\ Free
  /\ s' = [s EXCEPT !.fence = {a}, !.active[a] = Head(s.pq[a]),
                    !.target[a] = s.dg, !.pc[a] = "install"]
Install(a) ==
  /\ MutOk(a) /\ s.pc[a] = "install" /\ OwnFence(a)
  /\ LET x == s.active[a]
         g == s.target[a]
         t == TreeOf(g)
         writes == x = "T" \/ s.instGen # g \/ s.failedI
         early == Rec(x, g, TRUE, FALSE, FALSE)
         accepted == ~PublishAfterReadback /\ s.epoch[a] = s.brokerEpoch
     IN s' = [s EXCEPT !.pc[a] = "readback",
               !.mutNoTake = IF writes THEN Noted(a) ELSE @,
               !.trees[t] = IF x = "T" THEN "verified" ELSE @,
               !.tamper[t] = IF x = "T" THEN FALSE ELSE @,
               !.instGen = IF x = "I" /\ writes THEN g ELSE @,
               !.foreign = IF x = "I" /\ writes THEN FALSE ELSE @,
               !.wrote = IF x = "I" /\ writes THEN @ \cup {"reconciler"} ELSE @,
               !.bus = IF accepted THEN @ \cup
                          {BusRec(s.epoch[a], s.hostSeq[a] + 1, x, early)} ELSE @,
               !.pub[x] = IF accepted THEN early ELSE @,
               !.hostSeq[a] = IF accepted THEN @ + 1 ELSE @]
Readback(a) ==
  /\ MutOk(a) /\ s.pc[a] = "readback" /\ OwnFence(a)
  /\ LET x == s.active[a]
         g == s.target[a]
         treeOK == s.trees[TreeOf(g)] = "verified" /\ ~s.tamper[TreeOf(g)]
         mismatch == s.foreign \/ s.instGen # g
         good == IF x = "T" THEN treeOK ELSE ~(ReadbackCompares /\ mismatch)
         rec == Rec(x, g, good, TRUE, x = "I" /\ mismatch)
     IN s' = [s EXCEPT !.rb[a] = rec, !.pc[a] = "state-read",
                       !.failedI = IF x = "I" THEN ~good ELSE @]

CanRead(w) == s.wpc[w] = "idle" /\ (StateLock => s.lockHolder = NoHolder)
PassRead(a) ==
  /\ MutOk(a) /\ s.pc[a] = "state-read" /\ OwnFence(a)
  /\ CanRead(W(a, "pass"))
  /\ s' = [s EXCEPT !.wpc[W(a, "pass")] = "read",
                    !.wcopy[W(a, "pass")] = s.sf, !.pc[a] = "state-write",
                    !.lockHolder = IF StateLock THEN W(a, "pass") ELSE @]
PassWrite(a) ==
  /\ MutOk(a) /\ s.pc[a] = "state-write" /\ OwnFence(a)
  /\ s.wpc[W(a, "pass")] = "read"
  /\ StateLock => s.lockHolder = W(a, "pass")
  /\ LET copy == s.wcopy[W(a, "pass")]
     IN s' = [s EXCEPT !.sf = [copy EXCEPT !.m = @ + 1,
                                           !.records[s.active[a]] = s.rb[a]],
                       !.dm = @ + 1, !.wpc[W(a, "pass")] = "idle",
                       !.lockHolder = NoHolder, !.pc[a] = "release",
                       !.mutNoTake = Noted(a)]
Release(a) ==
  /\ MutOk(a) /\ s.pc[a] = "release" /\ OwnFence(a)
  /\ s' = [s EXCEPT !.fence = @ \ {a}, !.pc[a] = "after",
                    !.done[a] = @ \cup {s.active[a]}, !.pq[a] = Tail(@)]
Continue(a) ==
  /\ MutOk(a) /\ s.pc[a] = "after" /\ s.pq[a] # <<>>
  /\ s' = [s EXCEPT !.pc[a] = "ready"]
EndPass(a) ==
  /\ MutOk(a) /\ s.pc[a] = "after" /\ s.done[a] # {}
  /\ LET wait == [x \in Surf |-> IF x \in s.done[a] THEN 0
                                 ELSE Min2(s.since[a][x] + 1)]
     IN s' = [s EXCEPT !.since[a] = wait, !.done[a] = {},
                       !.pc[a] = IF Behind THEN "ready" ELSE "idle",
                       !.pq[a] = IF Behind THEN Order(wait) ELSE <<>>]

RepublishRead(a, x) ==
  /\ MutOk(a) /\ s.rpc[a] = "idle" /\ CanRead(W(a, "republish"))
  /\ s' = [s EXCEPT !.wpc[W(a, "republish")] = "read",
                    !.wcopy[W(a, "republish")] = s.sf,
                    !.reserve[a] = Max(s.sf.seq, s.hostSeq[a]) + 1,
                    !.rx[a] = x, !.rpc[a] = "write",
                    !.lockHolder = IF StateLock THEN W(a, "republish") ELSE @]
RepublishWrite(a) ==
  /\ MutOk(a) /\ s.rpc[a] = "write" /\ s.wpc[W(a, "republish")] = "read"
  /\ StateLock => s.lockHolder = W(a, "republish")
  /\ LET copy == s.wcopy[W(a, "republish")]
     IN s' = [s EXCEPT !.sf = [copy EXCEPT !.r = @ + 1, !.seq = s.reserve[a]],
                       !.dr = @ + 1, !.hostSeq[a] = s.reserve[a],
                       !.outgoing[a] = copy.records[s.rx[a]],
                       !.wpc[W(a, "republish")] = "idle", !.lockHolder = NoHolder,
                       !.rpc[a] = "publish", !.mutNoTake = Noted(a)]
Publish(a) ==
  /\ MutOk(a) /\ s.rpc[a] = "publish"
  /\ IF s.epoch[a] # s.brokerEpoch
        THEN s' = [Stop(a) EXCEPT !.publishFailed = @ \cup {a}]
        ELSE s' = [s EXCEPT !.pub[s.rx[a]] = s.outgoing[a], !.rpc[a] = "idle",
                           !.bus = @ \cup {BusRec(s.epoch[a], s.reserve[a],
                                                 s.rx[a], s.outgoing[a])}]

MaintenanceTake(a) ==
  /\ MutOk(a) /\ Free /\ ~s.maint[a]
  /\ s' = [s EXCEPT !.fence = {a}, !.maint[a] = TRUE]
MaintenanceRelease(a) ==
  /\ MutOk(a) /\ s.maint[a] /\ OwnFence(a)
  /\ s' = [s EXCEPT !.fence = @ \ {a}, !.maint[a] = FALSE]
Pinned(t) == \E l \in Lanes : s.pin[l].exists /\ s.pin[l].tree = t
Collect(a, t) ==
  /\ MutOk(a) /\ s.maint[a] /\ OwnFence(a)
  /\ t \in s.old /\ t # TreeOf(s.dg) /\ s.trees[t] # "absent"
  /\ s' = IF CollectRespectsPins /\ Pinned(t) THEN s
          ELSE [s EXCEPT !.trees[t] = "absent", !.mutNoTake = Noted(a),
                         !.removedPinned = @ \/ Pinned(t)]
PinValid(l) == s.liveStart[l] > 0 /\ s.pin[l].boot = 1
               /\ (PinChecksStart => s.pin[l].start = s.liveStart[l])
Sweep(a) ==
  /\ MutOk(a) /\ s.maint[a] /\ OwnFence(a)
  /\ LET pins == [l \in Lanes |-> [s.pin[l] EXCEPT
                                    !.exists = @ /\ PinValid(l)]]
     IN s' = [s EXCEPT !.pin = pins, !.mutNoTake = Noted(a),
                       !.orphanSurvived = @ \/
                         (\E l \in Lanes : pins[l].exists /\ s.lst[l] # "running")]

Plan(l) ==
  /\ s.lst[l] = "out" /\ s.pub["T"].status = "CONVERGED"
  /\ s' = [s EXCEPT !.lst[l] = "planned", !.plan[l] = s.pub["T"].tree]
Admit(l) ==
  /\ s.lst[l] = "planned" /\ Free
  /\ s' = [s EXCEPT !.lst[l] = "admitting", !.fence = {l}]
Launch(l) ==
  /\ s.lst[l] = "admitting" /\ OwnFence(l)
  /\ LET t == s.plan[l]
         good == s.trees[t] = "verified" /\ ~s.tamper[t]
     IN IF RecheckUnderFence /\ ~good
          THEN s' = [s EXCEPT !.lst[l] = "rejected", !.fence = @ \ {l}]
          ELSE s' = [s EXCEPT !.lst[l] = "running", !.fence = @ \ {l},
                       !.pin[l] = [exists |-> TRUE, tree |-> t, start |-> 1, boot |-> 1],
                       !.liveStart[l] = 1, !.badLaunch = @ \/ ~good]
RunnerCrash(l) ==
  /\ s.lst[l] = "admitting" /\ s.crashes < MaxCrash
  /\ s' = [s EXCEPT !.lst[l] = "admission-crashed", !.fence = @ \ {l},
                    !.crashes = @ + 1]
LaneEnd(l) ==
  /\ s.lst[l] = "running" /\ Free
  \* Pin deletion is one atomic fence transaction, with empty fence at both ends.
  /\ s' = [s EXCEPT !.lst[l] = "ended", !.pin[l].exists = FALSE, !.liveStart[l] = 0]
LaneCrash(l) ==
  /\ s.lst[l] = "running"
  /\ s' = [s EXCEPT !.lst[l] = "crashed", !.liveStart[l] = 0]
Reuse(l) ==
  /\ s.lst[l] = "crashed" /\ s.liveStart[l] = 0
  \* The lane's pid and boot are unchanged; the unrelated process has start=2.
  /\ s' = [s EXCEPT !.liveStart[l] = 2]

Foreign(w) == s' = [s EXCEPT !.foreign = TRUE, !.wrote = @ \cup {w}]
Guarded(w, attempt) ==
  IF OwnerGuard THEN s' = [s EXCEPT !.refused = @ \cup {attempt}]
  ELSE Foreign(w)
HandInstallTree == Guarded("hand", "hand install-tree")
HandHotPatch == Guarded("hand", "hand hot-patch")
FixInstallPluginTree == Guarded("fixg", "host-drift fix install-plugin-tree")
AdoptPluginTarget == Guarded("adopt", "adopt-merged plugin target")
FixUnguarded == IF UnguardedFixOn THEN Foreign("fixu") ELSE UNCHANGED s
UnknownWrite == /\ UnknownWriterOn /\ Foreign("unknown")
NewGeneration == /\ s.dg = 1 /\ s' = [s EXCEPT !.dg = 2]
Age(t) == s' = [s EXCEPT !.old = @ \cup {t}]
Tamper(t) == /\ Free /\ s.trees[t] = "verified"
             /\ s' = [s EXCEPT !.tamper[t] = TRUE]

Dispatch(e) ==
  CASE e.op = "boot" -> Boot(e.a)
    [] e.op = "role" -> RoleLock(e.a)
    [] e.op = "epoch" -> OpenEpoch(e.a)
    [] e.op = "recover" -> Recover(e.a)
    [] e.op = "process-crash" -> ProcessCrash(e.a)
    [] e.op = "begin" -> Begin(e.a)
    [] e.op = "take" -> Take(e.a)
    [] e.op = "install" -> Install(e.a)
    [] e.op = "readback" -> Readback(e.a)
    [] e.op = "pread" -> PassRead(e.a)
    [] e.op = "pwrite" -> PassWrite(e.a)
    [] e.op = "release" -> Release(e.a)
    [] e.op = "continue" -> Continue(e.a)
    [] e.op = "end" -> EndPass(e.a)
    [] e.op = "rread" -> RepublishRead(e.a, e.x)
    [] e.op = "rwrite" -> RepublishWrite(e.a)
    [] e.op = "publish" -> Publish(e.a)
    [] e.op = "mtake" -> MaintenanceTake(e.a)
    [] e.op = "mrelease" -> MaintenanceRelease(e.a)
    [] e.op = "collect" -> Collect(e.a, e.x)
    [] e.op = "sweep" -> Sweep(e.a)
    [] e.op = "plan" -> Plan(e.a)
    [] e.op = "admit" -> Admit(e.a)
    [] e.op = "launch" -> Launch(e.a)
    [] e.op = "runner-crash" -> RunnerCrash(e.a)
    [] e.op = "lane-end" -> LaneEnd(e.a)
    [] e.op = "lane-crash" -> LaneCrash(e.a)
    [] e.op = "reuse" -> Reuse(e.a)
    [] e.op = "hand-install" -> HandInstallTree
    [] e.op = "hand-patch" -> HandHotPatch
    [] e.op = "fix-guarded" -> FixInstallPluginTree
    [] e.op = "adopt" -> AdoptPluginTarget
    [] e.op = "fix-unguarded" -> FixUnguarded
    [] e.op = "unknown" -> UnknownWrite
    [] e.op = "generation" -> NewGeneration
    [] e.op = "age" -> Age(e.x)
    [] e.op = "tamper" -> Tamper(e.x)

Next ==
  /\ todo # <<>>
  /\ \E e \in Head(todo) :
       /\ Dispatch(e)
       /\ todo' = IF Head(todo) = {e} THEN Tail(todo)
                  ELSE <<Head(todo) \ {e}>> \o Tail(todo)
Spec == Init /\ [][Next]_vars

TypeOK ==
  /\ s.dg \in 1..2 /\ s.crashes \in 0..MaxCrash
  /\ s.fence \subseteq (Procs \cup Lanes) /\ Cardinality(s.fence) <= 1
  /\ s.roleLock \in Procs \cup {"none"}
  /\ s.lockHolder \in Writers \cup {NoHolder}
  /\ s.brokerEpoch \in 0..2
  /\ \A a \in Procs :
       /\ s.epoch[a] \in 0..s.brokerEpoch
       /\ s.rs[a] \in {"down", "booted", "locked", "opened", "up"}
       /\ \A x \in Surf : s.since[a][x] \in 0..2
  /\ s.wrote \subseteq Known \cup {"unknown"}
NoObservedBeforeReadback == \A b \in s.bus : b.rec.status = "CONVERGED" => b.rec.rbd
NoLaneOnUnverifiedTree == ~s.badLaunch
PinnedTreeNeverRemoved == ~s.removedPinned
OrphanPinReleased == ~s.orphanSurvived
OwnedSurfaceOneWriter == s.wrote \cap Known \subseteq {"reconciler"}
ForeignWriteReported == \A b \in s.bus : b.rec.status = "CONVERGED" => ~b.rec.fgn
TakeoverBeforeMutation == ~s.mutNoTake
StateWritesSerialised == s.sf.m = s.dm /\ s.sf.r = s.dr
EverySurfaceWithinTwoPasses == \A a \in Procs : \A x \in Surf : s.since[a][x] <= 1
=============================================================================
