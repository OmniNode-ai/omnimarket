---------------------------- MODULE ClaimRelease ------------------------------
(* Releasing a lane's PR claims when the lane ends: the writers of one claim file
   and the rule that decides who may delete it.

   A PR claim is one file per PR under the claims directory. It names its holder by
   an exact identity: lane, run and session together (one "instance" here). The old
   runner wrote claims and never released them. The new closeout (the stop node
   after an observed exit or a final receipt, and the lane closeout node) deletes
   the claims of one ended instance. So one file has more than one writer:
     the lane      acquire, heartbeat and its own release (while it is live)
     the closeout  release of an ended instance's claims, in two steps: inspect the
                   file (take a snapshot), then delete only the inspected file
     the reaper    expiry: a claim whose heartbeat lapsed is reaped by the next
                   acquirer, whoever holds it

   Instances i1 and i2 carry the SAME lane name: i2 is the replacement the
   orchestrator launches for i1 (the lane's name is reused, its run and session are
   new). Two PRs, p1 and p2. Time is abstract: Age is the heartbeat lapsing, in any
   order with every other step.

   The rule: a claim is released by anyone other than its holder only after the
   holder's lane has ended, and never while a live lane holds it.

   Properties (each is broken by its own mutation below):
     NeverReleaseLiveHolder     no release by a writer other than the holder happens
                                to a holder whose lane is live (closeout or expiry)
     ReleasedOnlyAfterExitSeen  a closeout release carries the observed exit of the
                                holder (a final receipt or a gone process tree)
     ClosesOwnOnly              a closeout deletes only a claim of the exact
                                identity it closes: a replacement that reuses the
                                lane name is not that identity
     DeletesWhatItInspected     a closeout deletes the file it inspected: a claim
                                that replaced it between the inspection and the
                                delete is preserved
     NoStrandedClaim            a claim whose holder ended, with its exit observed
                                and the closeout running, is eventually released
                                (liveness)
   Mutations (cfg constants) that must break them:
     RequireExit=FALSE          closeout without an observed exit   -> NeverReleaseLiveHolder,
                                                                       ReleasedOnlyAfterExitSeen
     ExactIdentity=FALSE        closeout matches by lane name       -> ClosesOwnOnly
     CompareBeforeDelete=FALSE  the delete takes what is there now  -> DeletesWhatItInspected
     LiveHeartbeats=FALSE       a live lane's heartbeat may lapse   -> NeverReleaseLiveHolder
     CloseoutRuns=FALSE         nothing releases an ended lane's claim -> NoStrandedClaim *)
EXTENDS Integers, FiniteSets

CONSTANTS ExactIdentity, RequireExit, CompareBeforeDelete, LiveHeartbeats, CloseoutRuns

Instances == {"i1", "i2"}
PRs == {"p1", "p2"}
NoClaim == "none"
LaneName(i) == "lane-x"   \* both instances are the same lane name

VARIABLES life,    \* instance -> "unborn" | "live" | "ended"
          claim,   \* PR -> NoClaim | the instance whose identity the file names
          stale,   \* PR -> the claim's heartbeat has lapsed
          seen,    \* instance -> closeout has observed its exit
          snap,    \* instance -> PR -> what closeout of that instance inspected
          rel      \* every release: [how, by, holder, life, seen, snapshot]

vars == <<life, claim, stale, seen, snap, rel>>

Holders == Instances \cup {NoClaim}

TypeOK ==
  /\ life \in [Instances -> {"unborn", "live", "ended"}]
  /\ claim \in [PRs -> Holders]
  /\ stale \in [PRs -> BOOLEAN]
  /\ seen \in [Instances -> BOOLEAN]
  /\ snap \in [Instances -> [PRs -> Holders]]
  /\ \A r \in rel : r.how \in {"self", "closeout", "expiry"}

Init ==
  /\ life = [i \in Instances |-> "unborn"]
  /\ claim = [p \in PRs |-> NoClaim]
  /\ stale = [p \in PRs |-> FALSE]
  /\ seen = [i \in Instances |-> FALSE]
  /\ snap = [i \in Instances |-> [p \in PRs |-> NoClaim]]
  /\ rel = {}

\* ---- the lane ---------------------------------------------------------------
Start(i) ==
  /\ life[i] = "unborn"
  /\ life' = [life EXCEPT ![i] = "live"]
  /\ UNCHANGED <<claim, stale, seen, snap, rel>>

Acquire(i, p) ==
  /\ life[i] = "live" /\ claim[p] = NoClaim
  /\ claim' = [claim EXCEPT ![p] = i]
  /\ stale' = [stale EXCEPT ![p] = FALSE]
  /\ UNCHANGED <<life, seen, snap, rel>>

Heartbeat(i, p) ==
  /\ life[i] = "live" /\ claim[p] = i
  /\ stale' = [stale EXCEPT ![p] = FALSE]
  /\ UNCHANGED <<life, claim, seen, snap, rel>>

SelfRelease(i, p) ==
  /\ life[i] = "live" /\ claim[p] = i
  /\ claim' = [claim EXCEPT ![p] = NoClaim]
  /\ rel' = rel \cup {[how |-> "self", by |-> i, holder |-> i, life |-> "live",
                       seen |-> seen[i], snapshot |-> i]}
  /\ UNCHANGED <<life, stale, seen, snap>>

End(i) ==   \* the engine exits or dies at any time; its claims are untouched
  /\ life[i] = "live"
  /\ life' = [life EXCEPT ![i] = "ended"]
  /\ UNCHANGED <<claim, stale, seen, snap, rel>>

\* ---- the reaper -------------------------------------------------------------
Age(p) ==   \* the heartbeat lapses; a heartbeating live lane never lapses
  /\ claim[p] # NoClaim /\ ~stale[p]
  /\ (~LiveHeartbeats \/ life[claim[p]] = "ended")
  /\ stale' = [stale EXCEPT ![p] = TRUE]
  /\ UNCHANGED <<life, claim, seen, snap, rel>>

Expire(p) ==
  /\ claim[p] # NoClaim /\ stale[p]
  /\ rel' = rel \cup {[how |-> "expiry", by |-> "reaper", holder |-> claim[p],
                       life |-> life[claim[p]], seen |-> seen[claim[p]],
                       snapshot |-> claim[p]]}
  /\ claim' = [claim EXCEPT ![p] = NoClaim]
  /\ stale' = [stale EXCEPT ![p] = FALSE]
  /\ UNCHANGED <<life, seen, snap>>

\* ---- the closeout -----------------------------------------------------------
Observe(i) ==   \* the final receipt or the gone process tree
  /\ life[i] = "ended" /\ ~seen[i]
  /\ seen' = [seen EXCEPT ![i] = TRUE]
  /\ UNCHANGED <<life, claim, stale, snap, rel>>

Matches(h, i) == IF ExactIdentity THEN h = i ELSE LaneName(h) = LaneName(i)

Inspect(i, p) ==
  /\ CloseoutRuns
  /\ RequireExit => seen[i]
  /\ claim[p] # NoClaim /\ Matches(claim[p], i)
  /\ snap[i][p] = NoClaim
  /\ snap' = [snap EXCEPT ![i][p] = claim[p]]
  /\ UNCHANGED <<life, claim, stale, seen, rel>>

Delete(i, p) ==
  /\ snap[i][p] # NoClaim
  /\ IF CompareBeforeDelete
       THEN IF claim[p] = snap[i][p]
              THEN /\ claim' = [claim EXCEPT ![p] = NoClaim]
                   /\ rel' = rel \cup {[how |-> "closeout", by |-> i, holder |-> snap[i][p],
                                        life |-> life[snap[i][p]], seen |-> seen[i],
                                        snapshot |-> snap[i][p]]}
              ELSE /\ UNCHANGED <<claim, rel>>   \* a replacement owns the file: kept
       ELSE IF claim[p] # NoClaim
              THEN /\ claim' = [claim EXCEPT ![p] = NoClaim]
                   /\ rel' = rel \cup {[how |-> "closeout", by |-> i, holder |-> claim[p],
                                        life |-> life[claim[p]], seen |-> seen[i],
                                        snapshot |-> snap[i][p]]}
              ELSE UNCHANGED <<claim, rel>>
  /\ snap' = [snap EXCEPT ![i][p] = NoClaim]
  /\ stale' = IF claim'[p] = NoClaim THEN [stale EXCEPT ![p] = FALSE] ELSE stale
  /\ UNCHANGED <<life, seen>>

Next ==
  \/ \E i \in Instances : Start(i) \/ End(i) \/ Observe(i)
  \/ \E i \in Instances, p \in PRs :
       Acquire(i, p) \/ Heartbeat(i, p) \/ SelfRelease(i, p)
         \/ Inspect(i, p) \/ Delete(i, p)
  \/ \E p \in PRs : Age(p) \/ Expire(p)

Spec ==
  /\ Init /\ [][Next]_vars
  /\ \A i \in Instances : WF_vars(Observe(i))
  /\ \A i \in Instances, p \in PRs : WF_vars(Inspect(i, p)) /\ WF_vars(Delete(i, p))

\* ---- properties -------------------------------------------------------------
NeverReleaseLiveHolder == \A r \in rel : r.how # "self" => r.life = "ended"
ReleasedOnlyAfterExitSeen == \A r \in rel : r.how = "closeout" => r.seen
ClosesOwnOnly == \A r \in rel : r.how = "closeout" => r.by = r.holder
DeletesWhatItInspected == \A r \in rel : r.how = "closeout" => r.holder = r.snapshot

NoStrandedClaim ==
  \A i \in Instances, p \in PRs :
    (claim[p] = i /\ life[i] = "ended") ~> (claim[p] # i)

\* ---- witnesses: each is violated exactly when the state is reachable -------
NeverCloseout == \A r \in rel : r.how # "closeout"
NeverExpiry == \A r \in rel : r.how # "expiry"
NeverReplacementHoldsAfterCloseout ==
  ~(\E p \in PRs : claim[p] = "i2" /\ life["i1"] = "ended" /\ seen["i1"]
       /\ \E r \in rel : r.how = "closeout" /\ r.by = "i1")
=============================================================================
