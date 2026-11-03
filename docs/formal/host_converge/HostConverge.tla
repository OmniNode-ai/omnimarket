--------------------------- MODULE HostConverge -----------------------------
(* One host under the converge mechanism: the reconciler (its long-lived process "ll"
   and its five-minute one-shot "os"), its republish task, the host mutation fence, the
   runner's lane admission, the lanes that pin a tree, and every writer of the installed
   plugin manifest, guarded and unguarded.

   The fence is the host's mutation fence: a set of holders, free when empty; a holder that
   dies releases its share at once (a file lock closes with its last descriptor). The
   reconciler takes it for each mutation; the runner takes it at admission and releases it
   once the lane is live. Every mutation of a lane tree, the installed manifest, the pins
   or the tree store happens under it.

   Surfaces: T the lane tree of the desired generation (built read-only, verified by a
   hash marker, rehashed on readback), I the installed plugin manifest (owned by the
   reconciler on a joined host). A pass handles each surface once, in the order of how
   long the surface has waited, reads the newest desired generation before each, and may
   end at its time budget after at least one surface; a surface's record is published
   only from a readback.

   Writers of the installed manifest:
     guarded, refused on an owned surface    hand install-tree (with its hot-patch writes),
                                             the host-drift fix's install-plugin-tree action,
                                             adopt-merged's plugin target
     unguarded                               the host-drift fix's `claude plugin` actions
                                             (UnguardedFixOn), and an unknown writer
                                             (UnknownWriterOn); neither can be stopped from here
   Lanes: placement reads the published record of the tree; the runner then takes the fence,
   rechecks the tree under it (rehash, not the published record), writes the pin and starts
   the lane. A lane ends (pin removed) or crashes (pin left); a pid can be reused by an
   unrelated process within a boot. The reconciler sweeps pins and collects aged trees.

   Invariants (each broken by its own mutant, a flipped constant of Model.cfg):
     NoObservedBeforeReadback    a published CONVERGED record comes from a readback
                                 (PublishAfterReadback = FALSE: published right after the install)
     NoLaneOnUnverifiedTree      a lane is started only on a verified, untampered tree
                                 (RecheckUnderFence = FALSE: the admission check is not repeated under the fence)
     PinnedTreeNeverRemoved      a tree with a pin is never removed
                                 (CollectRespectsPins = FALSE)
     OrphanPinReleased           a pin of a gone process does not survive a sweep, also
                                 when its pid was reused (PinChecksStart = FALSE: pid alone)
     OwnedSurfaceOneWriter       no known writer but the reconciler writes the owned
                                 manifest; holds only while every known unguarded actor is off
                                 (UnguardedFixOn = TRUE)
     ForeignWriteReported        a published CONVERGED record was read back with no foreign
                                 write in the manifest (ReadbackCompares = FALSE)
     TakeoverBeforeMutation      a reconciler process mutates only after its takeover
                                 (lock, producer fencing, host sequence checked against the bus)
                                 (TakeoverFirst = FALSE)
     StateWritesSerialised       no write of the state file overwrites another
                                 (StateLock = FALSE)
     EverySurfaceWithinTwoPasses a surface is handled in a pass or the next one, whatever
                                 the rate of new generations (OrderByWait = FALSE) *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS MaxCrash,
          RecheckUnderFence, PublishAfterReadback, CollectRespectsPins, PinChecksStart,
          UnguardedFixOn, UnknownWriterOn, ReadbackCompares, TakeoverFirst, StateLock, OrderByWait

Procs  == {"ll", "os"}
Surf   == {"T", "I"}
Trees  == {"t1", "t2"}
Lanes  == {"a"}
Writers == {"wm", "wrt"}               \* state-file writers: a reconciler pass's verdict, the republish task
TreeOf(g) == IF g = 1 THEN "t1" ELSE "t2"
NoRec == [status |-> "NONE", tree |-> "none", rbd |-> TRUE, fgn |-> FALSE]
Sf0 == [m |-> 0, seq |-> 0]
Min2(n) == IF n > 2 THEN 2 ELSE n

VARIABLE s

Init ==
  s = [dg |-> 1, fence |-> {}, rs |-> [a \in Procs |-> "down"],        pby |-> "none", pq |-> <<>>, ph |-> {}, since |-> [x \in Surf |-> 0],
       trees |-> [t \in Trees |-> "absent"], tamper |-> [t \in Trees |-> FALSE],
       old |-> [t \in Trees |-> FALSE],
       lst |-> [l \in Lanes |-> "out"], ltree |-> [l \in Lanes |-> "none"],
       plan |-> [l \in Lanes |-> "none"], pin |-> [l \in Lanes |-> FALSE],
       reused |-> [l \in Lanes |-> FALSE], survived |-> [l \in Lanes |-> FALSE],
       badLaunch |-> FALSE, pub |-> [x \in Surf |-> NoRec], rb |-> [x \in Surf |-> NoRec],
       fresh |-> [x \in Surf |-> FALSE], instGen |-> 0, foreign |-> FALSE,
       refused |-> FALSE, bad |-> FALSE, fixuDone |-> FALSE, mutNoTake |-> FALSE, crashes |-> 0,
       sf |-> Sf0, wpc |-> [w \in Writers |-> "idle"], wcopy |-> [w \in Writers |-> Sf0],
       wused |-> {}, lockHolder |-> "none", dm |-> 0, ds |-> 0]

Free == s.fence = {}
MutOk(a) == s.rs[a] = "up" \/ (~TakeoverFirst /\ s.rs[a] = "booted")
Noted(a) == s.mutNoTake \/ s.rs[a] # "up"     \* a mutation before the takeover is complete

\* ---- the desired generation and the tree store -------------------------------
NewGen == /\ s.dg < 2 /\ s' = [s EXCEPT !.dg = 2]
AgeTree(t) == /\ ~s.old[t] /\ s' = [s EXCEPT !.old[t] = TRUE]
TamperTree(t) == /\ s.trees[t] = "verified" /\ ~s.tamper[t] /\ s' = [s EXCEPT !.tamper[t] = TRUE]

\* ---- the reconciler processes ---------------------------------------------
Boot(a) == /\ s.rs[a] = "down" /\ (a = "os" => s.rs["ll"] = "down")   \* the one-shot runs only if the long-lived process is not
           /\ s' = [s EXCEPT !.rs[a] = "booted"]
Takeover(a) == /\ s.rs[a] = "booted" /\ s' = [s EXCEPT !.rs[a] = "up"]
ProcCrash(a) == /\ s.rs[a] # "down" /\ s.crashes < MaxCrash
                /\ s' = [s EXCEPT !.rs[a] = "down", !.fence = @ \ {a},
                                  !.crashes = @ + 1,
                                  !.pby = IF s.pby = a THEN "none" ELSE s.pby,
                                  !.pq = IF s.pby = a THEN <<>> ELSE s.pq,
                                  !.ph = IF s.pby = a THEN {} ELSE s.ph]

Order == IF OrderByWait /\ s.since["I"] > s.since["T"] THEN <<"I", "T">> ELSE <<"T", "I">>
StartPass(a) == /\ MutOk(a) /\ s.pby = "none"
                /\ s' = [s EXCEPT !.pby = a, !.pq = Order, !.ph = {}]

\* One surface under the fence: install, read back, and (as the design has it) leave the
\* record for the publication step; the newest desired generation is read here.
Handle(a) ==
  /\ MutOk(a) /\ s.pby = a /\ s.pq # <<>> /\ Free
  /\ LET x == Head(s.pq)
         t == TreeOf(s.dg)
         bad == s.tamper[t] \/ s.trees[t] = "failed"
         built == IF s.trees[t] = "absent" THEN "verified" ELSE s.trees[t]
         newTrees == [s.trees EXCEPT ![t] = IF bad THEN "failed" ELSE built]
         need == s.instGen < s.dg \/ s.rb["I"].status = "FAILED"
         fg == IF need THEN FALSE ELSE s.foreign
         recT == [status |-> IF bad THEN "FAILED" ELSE "CONVERGED", tree |-> t, rbd |-> TRUE, fgn |-> FALSE]
         recI == [status |-> IF ReadbackCompares /\ fg THEN "FAILED" ELSE "CONVERGED",
                  tree |-> "none", rbd |-> TRUE, fgn |-> fg]
         rec == IF x = "T" THEN recT ELSE recI
         optimistic == [status |-> "CONVERGED", tree |-> rec.tree, rbd |-> FALSE, fgn |-> FALSE]
     IN s' = [s EXCEPT !.mutNoTake = Noted(a),
                       !.trees = IF x = "T" THEN newTrees ELSE s.trees,
                       !.instGen = IF x = "I" /\ need THEN s.dg ELSE s.instGen,
                       !.foreign = IF x = "I" THEN fg ELSE s.foreign,
                       !.rb[x] = rec, !.fresh[x] = TRUE,
                       !.pub[x] = IF PublishAfterReadback THEN s.pub[x] ELSE optimistic,
                       !.ph = @ \cup {x}, !.since[x] = 0, !.pq = Tail(s.pq)]

\* The publication of a record already read back.
Publish(x) == /\ s.fresh[x] /\ (s.rs["ll"] # "down" \/ s.rs["os"] # "down")
              /\ s' = [s EXCEPT !.pub[x] = [s.rb[x] EXCEPT !.rbd = TRUE], !.fresh[x] = FALSE]

\* A pass ends when every surface was handled, or at its budget after at least one.
PassEnd(a) == /\ s.pby = a /\ (s.pq = <<>> \/ s.ph # {})
              /\ s' = [s EXCEPT !.since = [x \in Surf |-> IF x \in s.ph THEN 0 ELSE Min2(s.since[x] + 1)],
                                !.pby = "none", !.pq = <<>>, !.ph = {}]

Collect(a) == /\ MutOk(a) /\ Free
              /\ \E t \in Trees :
                   /\ s.old[t] /\ s.trees[t] \in {"verified", "failed"} /\ t # TreeOf(s.dg)
                   /\ CollectRespectsPins => \A l \in Lanes : ~(s.pin[l] /\ s.ltree[l] = t)
                   /\ s' = [s EXCEPT !.trees[t] = "absent", !.mutNoTake = Noted(a)]

\* A pin is valid while its process runs: the pid, its start time and the boot id all match.
\* A reused pid keeps the pid but not the start time.
PinValid(l) == s.lst[l] = "running" \/ (~PinChecksStart /\ s.reused[l])
PinSweep(a) == /\ MutOk(a) /\ Free
               /\ LET np == [l \in Lanes |-> s.pin[l] /\ PinValid(l)]
                  IN s' = [s EXCEPT !.pin = np, !.mutNoTake = Noted(a),
                                    !.survived = [l \in Lanes |-> s.survived[l] \/ (np[l] /\ s.lst[l] # "running")]]

\* ---- the runner and its lanes -----------------------------------------------
PlaceLane(l, t) == /\ s.lst[l] = "out" /\ s.pub["T"].status = "CONVERGED" /\ s.pub["T"].tree = t
                   /\ s' = [s EXCEPT !.lst[l] = "planned", !.plan[l] = t]
Admit(l) == /\ s.lst[l] = "planned" /\ Free
            /\ s' = [s EXCEPT !.lst[l] = "admitting", !.fence = {l}]
AdmitCrash(l) == /\ s.lst[l] = "admitting" /\ s.crashes < MaxCrash
                 /\ s' = [s EXCEPT !.lst[l] = "out", !.fence = @ \ {l}, !.crashes = @ + 1]
Place(l) ==
  /\ s.lst[l] = "admitting"
  /\ LET t == s.plan[l]
         okNow == s.trees[t] = "verified" /\ ~s.tamper[t]
     IN IF RecheckUnderFence /\ ~okNow
          THEN s' = [s EXCEPT !.lst[l] = "out", !.fence = @ \ {l}]
          ELSE s' = [s EXCEPT !.lst[l] = "running", !.pin[l] = TRUE, !.ltree[l] = t,
                              !.fence = @ \ {l}, !.badLaunch = @ \/ ~okNow]
EndLane(l) == /\ s.lst[l] = "running" /\ s' = [s EXCEPT !.lst[l] = "ended", !.pin[l] = FALSE]
CrashLane(l) == /\ s.lst[l] = "running" /\ s' = [s EXCEPT !.lst[l] = "crashed"]
ReusePid(l) == /\ s.lst[l] = "crashed" /\ ~s.reused[l] /\ s' = [s EXCEPT !.reused[l] = TRUE]

\* ---- writers of the installed manifest --------------------------------------
GuardedWriter(w) == s' = [s EXCEPT !.refused = TRUE]       \* refused on the owned surface, naming the owner
FixUnguarded == /\ UnguardedFixOn /\ ~s.fixuDone
                /\ s' = [s EXCEPT !.fixuDone = TRUE, !.foreign = TRUE, !.bad = TRUE]
UnknownWrite == /\ UnknownWriterOn /\ ~s.foreign
                /\ s' = [s EXCEPT !.foreign = TRUE]

\* ---- the state file: read, then replace, by three writers ---------------------
WRead(w) == /\ w \notin s.wused /\ s.wpc[w] = "idle" /\ (StateLock => s.lockHolder = "none")
            /\ s' = [s EXCEPT !.wpc[w] = "read", !.wcopy[w] = s.sf,
                              !.lockHolder = IF StateLock THEN w ELSE @]
WWrite(w) == /\ s.wpc[w] = "read"
             /\ s' = [s EXCEPT !.wpc[w] = "idle", !.wused = @ \cup {w}, !.lockHolder = "none",
                               !.sf = IF w = "wrt" THEN [s.wcopy[w] EXCEPT !.seq = @ + 1]
                                                   ELSE [s.wcopy[w] EXCEPT !.m = @ + 1],
                               !.ds = IF w = "wrt" THEN @ + 1 ELSE @,
                               !.dm = IF w = "wrt" THEN @ ELSE @ + 1]

Next == NewGen \/ (AgeTree("t1") \/ TamperTree("t1"))
        \/ (\E a \in Procs : Boot(a) \/ Takeover(a) \/ ProcCrash(a) \/ StartPass(a) \/ Handle(a)
                             \/ PassEnd(a) \/ Collect(a) \/ PinSweep(a))
        \/ (\E x \in Surf : Publish(x))
        \/ (\E l \in Lanes : (\E t \in Trees : PlaceLane(l, t)) \/ Admit(l) \/ AdmitCrash(l) \/ Place(l)
                             \/ EndLane(l) \/ CrashLane(l) \/ ReusePid(l))
        \/ (\E w \in {"hand", "fixg", "adopt"} : GuardedWriter(w)) \/ FixUnguarded \/ UnknownWrite
        \/ (\E w \in Writers : WRead(w) \/ WWrite(w))

Spec == Init /\ [][Next]_s

\* ---- invariants -------------------------------------------------------------
TypeOK ==
  /\ s.dg \in 1..2 /\ s.fence \subseteq Lanes /\ s.crashes \in 0..MaxCrash
  /\ \A x \in Surf : s.since[x] \in 0..2

NoObservedBeforeReadback == \A x \in Surf : s.pub[x].status = "CONVERGED" => s.pub[x].rbd

NoLaneOnUnverifiedTree == ~s.badLaunch

PinnedTreeNeverRemoved == \A l \in Lanes : s.pin[l] => s.trees[s.ltree[l]] # "absent"

OrphanPinReleased == \A l \in Lanes : ~s.survived[l]

OwnedSurfaceOneWriter == ~s.bad          \* no known writer but the reconciler wrote the manifest

ForeignWriteReported == \A x \in Surf : s.pub[x].status = "CONVERGED" => ~s.pub[x].fgn

TakeoverBeforeMutation == ~s.mutNoTake

StateWritesSerialised ==
  (\A w \in Writers : s.wpc[w] = "idle") => (s.sf.m = s.dm /\ s.sf.seq = s.ds)

EverySurfaceWithinTwoPasses == \A x \in Surf : s.since[x] <= 1
=============================================================================
