---------------------------- MODULE CreateTicketDraft ----------------------------
(***************************************************************************)
(* The ticket node's draft create (OMN-23760, plan task S3): make the      *)
(* issue, take its id, build the contract (repository from the request),   *)
(* set its fingerprint, render it, write the description, publish          *)
(* onex.evt.omnimarket.ticket-contract-written.v1, answer with one         *)
(* terminal.                                                               *)
(*                                                                         *)
(* WHAT IS MODELLED                                                        *)
(*  - Linear is a store of issues. An issue has a cid (the DRAFT's own     *)
(*    correlation id, carried on the request; not the bus envelope's, which*)
(*    changes on a retry), the creating run (by), a stamped flag (the cid  *)
(*    is visible in the issue), proj (Linear copied the parent's project), *)
(*    wrote (the description write took effect) and content: "stub",       *)
(*    "block_norm" (what Linear RETURNS for the written block; Linear      *)
(*    normalises markdown, so what was sent, "block_sent", is never what   *)
(*    comes back) or "edited" (a person changed it by hand).               *)
(*  - A run is one delivery of one command. Its steps are find, create,    *)
(*    relook, stamp, clear, build (memory only), write, readback, publish, *)
(*    term, ack. A run may crash between any two steps; the process dies, *)
(*    memory is lost, Linear and the bus keep what they hold.              *)
(*  - The bus is at least once: a command not acknowledged is delivered    *)
(*    again after a crash (pending, Redeliver). The caller may retry and   *)
(*    the bus may deliver one message twice, in parallel with the first    *)
(*    run (extra, up to MaxExtra per cid). Every Dup is a new envelope     *)
(*    with the same draft id.                                              *)
(*  - A Linear call may fail; a create and a write may fail AFTER taking   *)
(*    effect. A lookup may miss an issue that exists (FindMayMiss).        *)
(*    A terminal may fail to publish (counted in the crash budget).        *)
(*  - A person may edit an issue at any time (HandEdit, MaxEdit).          *)
(*  - The written event is keyed by (cid, issue, fingerprint, hash): a     *)
(*    second publish of the same key is the same event (events is a set).  *)
(*                                                                         *)
(* THE DESIGN (what Model.cfg checks)                                      *)
(*  D1 the issue id is derived from the draft id and sent with the create  *)
(*     (IdLookup): a second create for the same draft is refused by Linear *)
(*     and the run looks the issue up by id: no search, no race. ASSUMES   *)
(*     Linear accepts a client id and refuses a duplicate: unmeasured.     *)
(*  D2 the stamp is written in the create call itself (StampAtomic) and the*)
(*     description write keeps it (WriteKeepsStamp).                       *)
(*  D3 after a refused create, exactly one lookup; a miss ends the run with*)
(*     the typed reason id_unreadable (ConflictOneLookup).                 *)
(*  D4 every run, fresh or rerun, clears the inherited project before the  *)
(*     write (ResumeClears).                                               *)
(*  D5 a rerun that finds the issue with no block builds and writes; one   *)
(*     that finds the block reads back and publishes again                 *)
(*     (ResumeChecksBlock, ResumeRepublishes); one that finds an edited    *)
(*     issue fails with hand_edited and writes nothing.                    *)
(*  D6 readback must return the block built (ReadChecksBlock); the event   *)
(*     hashes what readback returned (HashFromLinear), only after the      *)
(*     write (WriteBeforePublish); its id is the key, not the run          *)
(*     (EventIdPerCid); the fingerprint is a function of the draft and the *)
(*     issue only, no clock (FpStable).                                    *)
(*  D7 every failure ends the run with one typed failed terminal           *)
(*     (EmitOnError); completed is emitted after the written event.        *)
(*  D8 the consumer acknowledges only after the terminal is published      *)
(*     (Redeliver): a run that did not publish its terminal is delivered   *)
(*     again.                                                              *)
(*  An alternative for a Linear that refuses a client id: search by stamp  *)
(*  with the runs of one draft never overlapping (Serial), a consistent    *)
(*  filter (FindMayMiss = FALSE) and the stamp atomic: alt_stamp_serial.cfg*)
(*                                                                         *)
(* NOT MODELLED: the contract's content; the guard and the lane stamp      *)
(* (the description write must keep the caller's first-line stamp and the  *)
(* guard must judge the final rendered text); the title hash apart from    *)
(* the description (one content value stands for both); the bus front's    *)
(* in-process replay cache (assumed empty, as after a restart); a hand     *)
(* edit landing between a run's find and its write (the write wins: the    *)
(* node's own new issue, seconds old); the consumer commit mode in the     *)
(* deployed runtime; time.                                                 *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Cids, MaxExtra, MaxCrash, MaxFail, MaxEdit,
          Serial, IdLookup, StampAtomic, FindMayMiss,
          ResumeChecksBlock, ResumeRepublishes, EmitOnError,
          WriteBeforePublish, HashFromLinear, EventIdPerCid, Redeliver,
          ReadChecksBlock, ConflictOneLookup, ResumeClears, FpStable,
          WriteKeepsStamp

VARIABLES issues, nextId, pc, iss, rh, tcount, terms, events,
          pending, extra, crashes, fails, edits, reads

vars == <<issues, nextId, pc, iss, rh, tcount, terms, events,
          pending, extra, crashes, fails, edits, reads>>

Sym == Permutations(Cids)
K == 1 + MaxExtra + MaxCrash
Ks == 1..K
Slots == Cids \X Ks
NoHash == "none"
\* the fingerprint of the contract built for draft c; with FpStable = FALSE it varies per run
Fp(c, k) == IF FpStable THEN <<"fp", c>> ELSE <<"fp", c, k>>

Active(s) == pc[s] \notin {"none", "done", "crashed", "unacked"}
AnyActive(c) == \E k \in Ks : Active(<<c, k>>)
NextFree(c, k) == /\ pc[<<c, k>>] = "none"
                  /\ \A j \in 1..(k - 1) : pc[<<c, j>>] # "none"

Found(c) == {i \in issues : i.cid = c /\ (IdLookup \/ i.stamped)}
SeenSet(c) == IF FindMayMiss THEN {Found(c), {}} ELSE {Found(c)}
FailPcs == {"fail", "fail_edited", "fail_unreadable"}
Reason(q) == IF q = "fail_edited" THEN "hand_edited"
             ELSE IF q = "fail_unreadable" THEN "id_unreadable" ELSE "linear_error"
\* where a run that has found issue i goes next (design D3, D7, D8)
Cont(i) == IF ~ResumeChecksBlock THEN "term"
           ELSE IF i.content = "edited" THEN "fail_edited"
           ELSE IF i.content = "block_norm" THEN (IF ResumeRepublishes THEN "readback" ELSE "term")
           ELSE (IF ResumeClears THEN "clear" ELSE "build")
NewIssue(s) == [id |-> nextId, cid |-> s[1], by |-> s, stamped |-> StampAtomic, proj |-> TRUE,
                content |-> "stub", wrote |-> FALSE]
Content(id) == (CHOOSE i \in issues : i.id = id).content
Wrote(id) == {IF i.id = id THEN [i EXCEPT !.content = "block_norm", !.wrote = TRUE,
                                        !.stamped = (i.stamped /\ WriteKeepsStamp)] ELSE i : i \in issues}
SetStamped(id) == {IF i.id = id THEN [i EXCEPT !.stamped = TRUE] ELSE i : i \in issues}

Init ==
  /\ issues = {}
  /\ nextId = 1
  /\ pc = [s \in Slots |-> "none"]
  /\ iss = [s \in Slots |-> 0]
  /\ rh = [s \in Slots |-> NoHash]
  /\ tcount = [s \in Slots |-> 0]
  /\ terms = {}
  /\ events = {}
  /\ pending = [c \in Cids |-> 1]
  /\ extra = [c \in Cids |-> 0]
  /\ crashes = 0
  /\ fails = 0
  /\ edits = 0
  /\ reads = {}


\* --- deliveries -----------------------------------------------------------
\* In every action UNCHANGED names the variables the action leaves alone.
Start(c, k) ==
  /\ NextFree(c, k) /\ pending[c] > 0
  /\ (~Serial \/ ~AnyActive(c))
  /\ pc' = [pc EXCEPT ![<<c, k>>] = "find"]
  /\ pending' = [pending EXCEPT ![c] = @ - 1]
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, extra, crashes, fails, edits, reads>>

\* a duplicate delivery or a caller retry with the same draft id (a new envelope, a new
\* correlation id on the wire, the same draft correlation id)
Dup(c, k) ==
  /\ NextFree(c, k) /\ extra[c] < MaxExtra
  /\ (~Serial \/ ~AnyActive(c))
  /\ pc' = [pc EXCEPT ![<<c, k>>] = "find"]
  /\ extra' = [extra EXCEPT ![c] = @ + 1]
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, crashes, fails, edits, reads>>

\* a failure of a Linear or bus call
GoFail(s) ==
  /\ fails < MaxFail /\ fails' = fails + 1
  /\ pc' = [pc EXCEPT ![s] = "fail"]

\* --- find (a lookup by the derived id, or a search by the stamp) ----------
FindOk(s) ==
  /\ pc[s] = "find"
  /\ \E seen \in SeenSet(s[1]) :
       IF seen = {}
       THEN /\ pc' = [pc EXCEPT ![s] = "create"]
            /\ UNCHANGED iss
       ELSE LET i == CHOOSE x \in seen : \A y \in seen : x.id <= y.id
            IN /\ iss' = [iss EXCEPT ![s] = i.id]
               /\ pc' = [pc EXCEPT ![s] = Cont(i)]
  /\ UNCHANGED <<issues, nextId, rh, tcount, terms, events, pending, extra, crashes, fails, edits, reads>>

FindFail(s) ==
  /\ pc[s] = "find" /\ GoFail(s)
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

\* --- create ---------------------------------------------------------------
Conflict(c) == IdLookup /\ \E i \in issues : i.cid = c

CreateOk(s) ==
  /\ pc[s] = "create"
  /\ IF Conflict(s[1])
     THEN /\ pc' = [pc EXCEPT ![s] = IF ConflictOneLookup THEN "relook" ELSE "find"]
          /\ UNCHANGED <<issues, nextId, iss>>
     ELSE /\ issues' = issues \cup {NewIssue(s)}
          /\ nextId' = nextId + 1
          /\ iss' = [iss EXCEPT ![s] = nextId]
          /\ pc' = [pc EXCEPT ![s] = IF StampAtomic THEN "clear" ELSE "stamp"]
  /\ UNCHANGED <<rh, tcount, terms, events, pending, extra, crashes, fails, edits, reads>>

CreateFailBefore(s) ==
  /\ pc[s] = "create" /\ GoFail(s)
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

CreateFailAfter(s) ==
  /\ pc[s] = "create" /\ GoFail(s)
  /\ IF Conflict(s[1])
     THEN UNCHANGED <<issues, nextId>>
     ELSE /\ issues' = issues \cup {NewIssue(s)}
          /\ nextId' = nextId + 1
  /\ UNCHANGED <<iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

\* --- after a refused create: exactly one lookup, a typed failure on a miss (R4)
RelookOk(s) ==
  /\ pc[s] = "relook"
  /\ \E seen \in SeenSet(s[1]) :
       IF seen = {}
       THEN /\ pc' = [pc EXCEPT ![s] = "fail_unreadable"]
            /\ UNCHANGED iss
       ELSE LET i == CHOOSE x \in seen : \A y \in seen : x.id <= y.id
            IN /\ iss' = [iss EXCEPT ![s] = i.id]
               /\ pc' = [pc EXCEPT ![s] = Cont(i)]
  /\ UNCHANGED <<issues, nextId, rh, tcount, terms, events, pending, extra, crashes, fails, edits, reads>>

\* --- stamp (only when it is a second call) --------------------------------
StampOk(s) ==
  /\ pc[s] = "stamp"
  /\ issues' = SetStamped(iss[s])
  /\ pc' = [pc EXCEPT ![s] = "clear"]
  /\ UNCHANGED <<nextId, iss, rh, tcount, terms, events, pending, extra, crashes, fails, edits, reads>>

StampFail(s) ==
  /\ pc[s] = "stamp" /\ GoFail(s)
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

\* --- clear the project Linear copied from the parent (OMN-21723), idempotent
ClearOk(s) ==
  /\ pc[s] = "clear"
  /\ issues' = {IF i.id = iss[s] THEN [i EXCEPT !.proj = FALSE] ELSE i : i \in issues}
  /\ pc' = [pc EXCEPT ![s] = "build"]
  /\ UNCHANGED <<nextId, iss, rh, tcount, terms, events, pending, extra, crashes, fails, edits, reads>>

ClearFail(s) ==
  /\ pc[s] = "clear" /\ GoFail(s)
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

\* --- build: contract, fingerprint, render; memory only --------------------
Build(s) ==
  /\ pc[s] = "build"
  /\ pc' = [pc EXCEPT ![s] = IF WriteBeforePublish THEN "write" ELSE "readback"]
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, fails, edits, reads>>

\* --- write the description (an update: the last writer wins) --------------
WriteOk(s) ==
  /\ pc[s] = "write"
  /\ issues' = Wrote(iss[s])
  /\ pc' = [pc EXCEPT ![s] = IF WriteBeforePublish THEN "readback" ELSE "term"]
  /\ UNCHANGED <<nextId, iss, rh, tcount, terms, events, pending, extra, crashes, fails, edits, reads>>

WriteFailBefore(s) ==
  /\ pc[s] = "write" /\ GoFail(s)
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

WriteFailAfter(s) ==
  /\ pc[s] = "write" /\ GoFail(s)
  /\ issues' = Wrote(iss[s])
  /\ UNCHANGED <<nextId, iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

\* --- read back what Linear returned; the block must be the one built (R1) --
ReadOk(s) ==
  /\ pc[s] = "readback"
  /\ LET got == Content(iss[s]) IN
     /\ reads' = reads \cup {[id |-> iss[s], hash |-> got]}
     /\ IF ReadChecksBlock /\ got # "block_norm"
        THEN /\ pc' = [pc EXCEPT ![s] = "fail_edited"]
             /\ UNCHANGED rh
        ELSE /\ rh' = [rh EXCEPT ![s] = IF HashFromLinear THEN got ELSE "block_sent"]
             /\ pc' = [pc EXCEPT ![s] = "publish"]
  /\ UNCHANGED <<issues, nextId, iss, tcount, terms, events, pending, extra, crashes, fails, edits>>

ReadFail(s) ==
  /\ pc[s] = "readback" /\ GoFail(s)
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

\* --- publish the written event (deterministic event id: the set key) -------
PublishOk(s) ==
  /\ pc[s] = "publish"
  /\ events' = events \cup {[cid |-> s[1], id |-> iss[s], fp |-> Fp(s[1], s[2]), hash |-> rh[s],
                             who |-> IF EventIdPerCid THEN 0 ELSE s[2]]}
  /\ pc' = [pc EXCEPT ![s] = IF WriteBeforePublish THEN "term" ELSE "write"]
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, pending, extra, crashes, fails, edits, reads>>

PublishFail(s) ==
  /\ pc[s] = "publish" /\ GoFail(s)
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, edits, reads>>

\* --- the terminal, then the ack -------------------------------------------
Term(s) ==
  /\ pc[s] = "term"
  /\ tcount' = [tcount EXCEPT ![s] = @ + 1]
  /\ terms' = terms \cup {[cid |-> s[1], kind |-> "completed", id |-> iss[s], fp |-> Fp(s[1], s[2]),
                           reason |-> "none", late |-> FALSE, seen |-> rh[s]]}
  /\ pc' = [pc EXCEPT ![s] = "ack"]
  /\ UNCHANGED <<issues, nextId, iss, rh, events, pending, extra, crashes, fails, edits, reads>>

FailTerm(s) ==
  /\ pc[s] \in FailPcs
  /\ IF EmitOnError
     THEN /\ tcount' = [tcount EXCEPT ![s] = @ + 1]
          /\ terms' = terms \cup {[cid |-> s[1], kind |-> "failed", id |-> 0, fp |-> NoHash,
                                   reason |-> Reason(pc[s]), seen |-> NoHash,
                                   late |-> \E t \in terms : t.cid = s[1] /\ t.kind = "completed"]}
          /\ pc' = [pc EXCEPT ![s] = "ack"]
     ELSE /\ pc' = [pc EXCEPT ![s] = "done"]
          /\ UNCHANGED <<tcount, terms>>
  /\ UNCHANGED <<issues, nextId, iss, rh, events, pending, extra, crashes, fails, edits, reads>>

\* the terminal cannot be published (counted in the crash budget: the run ends with no
\* terminal and no ack, the bus owes a delivery) (R7)
TermPubFail(s) ==
  /\ pc[s] \in ({"term"} \cup FailPcs)
  /\ crashes < MaxCrash /\ crashes' = crashes + 1
  /\ pc' = [pc EXCEPT ![s] = "unacked"]
  /\ pending' = [pending EXCEPT ![s[1]] = IF Redeliver THEN @ + 1 ELSE @]
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, extra, fails, edits, reads>>

Ack(s) ==
  /\ pc[s] = "ack"
  /\ pc' = [pc EXCEPT ![s] = "done"]
  /\ UNCHANGED <<issues, nextId, iss, rh, tcount, terms, events, pending, extra, crashes, fails, edits, reads>>

\* --- a crash between any two steps: memory lost, the bus owes a delivery --
Crash(s) ==
  /\ crashes < MaxCrash
  /\ Active(s)
  /\ pc' = [pc EXCEPT ![s] = "crashed"]
  /\ iss' = [iss EXCEPT ![s] = 0]
  /\ rh' = [rh EXCEPT ![s] = NoHash]
  /\ crashes' = crashes + 1
  /\ pending' = [pending EXCEPT ![s[1]] = IF Redeliver THEN @ + 1 ELSE @]
  /\ UNCHANGED <<issues, nextId, tcount, terms, events, extra, fails, edits, reads>>

\* --- a person edits the issue by hand (the written event exists to show it)
HandEdit ==
  /\ edits < MaxEdit
  /\ \E i \in issues :
       /\ i.content # "edited"
       /\ issues' = {IF x.id = i.id THEN [x EXCEPT !.content = "edited"] ELSE x : x \in issues}
  /\ edits' = edits + 1
  /\ UNCHANGED <<nextId, pc, iss, rh, tcount, terms, events, pending, extra, crashes, fails, reads>>

SlotStep(s) ==
  \/ FindOk(s) \/ FindFail(s)
  \/ CreateOk(s) \/ CreateFailBefore(s) \/ CreateFailAfter(s)
  \/ RelookOk(s)
  \/ StampOk(s) \/ StampFail(s)
  \/ ClearOk(s) \/ ClearFail(s)
  \/ Build(s)
  \/ WriteOk(s) \/ WriteFailBefore(s) \/ WriteFailAfter(s)
  \/ ReadOk(s) \/ ReadFail(s)
  \/ PublishOk(s) \/ PublishFail(s)
  \/ Term(s) \/ FailTerm(s) \/ TermPubFail(s) \/ Ack(s)

Next ==
  \/ \E s \in Slots : SlotStep(s) \/ Crash(s)
  \/ \E c \in Cids, k \in Ks : Start(c, k) \/ Dup(c, k)
  \/ HandEdit

Fairness ==
  /\ \A s \in Slots : WF_vars(SlotStep(s))
  /\ \A c \in Cids, k \in Ks : WF_vars(Start(c, k))

Spec == Init /\ [][Next]_vars
LiveSpec == Spec /\ Fairness

\* --- properties -----------------------------------------------------------
\* P1 one draft, one issue
AtMostOneIssuePerCid ==
  \A c \in Cids : Cardinality({i \in issues : i.cid = c}) <= 1

\* P2 a run emits at most one terminal, and a run that ends has emitted one
OneTerminalPerRun == \A s \in Slots : tcount[s] <= 1
DoneHasTerminal == \A s \in Slots : pc[s] \in {"ack", "done"} => tcount[s] = 1
TerminalsTyped ==
  \A t \in terms :
    /\ (t.kind = "completed" => (t.id # 0 /\ t.fp = Fp(t.cid, 1)))
    /\ (t.kind = "failed" => t.reason \in {"linear_error", "hand_edited", "id_unreadable"})

\* every command is answered: a run ends, and the command has a terminal in the log
RunEnds == \A s \in Slots : (Active(s)) ~> (pc[s] \in {"done", "crashed", "unacked"})
CommandAnswered == \A c \in Cids : <>(\E t \in terms : t.cid = c)

\* P3 the written event follows a successful description write, hashes what Linear
\* returned at the read it was made from, and that read held the block
WrittenFollowsWrite ==
  \A e \in events : \E i \in issues : i.id = e.id /\ i.cid = e.cid /\ i.wrote
WrittenHashesReturned ==
  \A e \in events : [id |-> e.id, hash |-> e.hash] \in reads
WrittenCertifiesBlock == \A e \in events : e.hash = "block_norm"
OneWrittenPerCid == \A c \in Cids : Cardinality({e \in events : e.cid = c}) <= 1

\* P4 success is never reported for an issue with no block (what the read returned held
\* the block and the write happened), not before the event, not with the parent's project
CompletedHasBlock ==
  \A t \in terms : t.kind = "completed" =>
    (t.seen = "block_norm" /\ \E i \in issues : i.id = t.id /\ i.cid = t.cid /\ i.wrote)
CompletedHasWritten ==
  \A t \in terms : t.kind = "completed" =>
    \E e \in events : e.cid = t.cid /\ e.id = t.id /\ e.fp = t.fp
CompletedNoProject ==
  \A t \in terms : t.kind = "completed" => \E i \in issues : i.id = t.id /\ ~i.proj

\* hazards the design accepts
NoFailAfterComplete == \A t \in terms : ~(t.kind = "failed" /\ t.late)
BlockHasEventWhenQuiet ==
  \A i \in issues : (i.wrote /\ ~AnyActive(i.cid) /\ pending[i.cid] = 0 /\ extra[i.cid] = MaxExtra)
                      => \E e \in events : e.cid = i.cid

\* --- witnesses: each says a state is unreachable; a violation shows it is reachable
NeverCompleted == \A t \in terms : t.kind # "completed"
NeverFailed == \A t \in terms : t.kind # "failed"
NeverHandEdited == \A t \in terms : t.reason # "hand_edited"
NeverIdUnreadable == \A t \in terms : t.reason # "id_unreadable"
NeverRerunCompletes ==
  ~(\E s \in Slots : s[2] >= 2 /\ pc[s] = "ack" /\ crashes >= 1 /\ iss[s] # 0
      /\ \E t \in terms : t.cid = s[1] /\ t.kind = "completed")
\* a rerun completes an issue whose creating run failed after the create took effect
NeverCreateFailAfterThenRerun ==
  ~(\E s \in Slots, i \in issues :
      pc[s] = "ack" /\ iss[s] = i.id /\ i.by # s /\ pc[i.by] = "done" /\ iss[i.by] = 0
      /\ \E t \in terms : t.cid = s[1] /\ t.kind = "completed")
NeverTwoActive ==
  ~(\E c \in Cids, j \in Ks, k \in Ks : j < k /\ Active(<<c, j>>) /\ Active(<<c, k>>))
NeverReplayPublish ==
  ~(\E s \in Slots : s[2] >= 2 /\ pc[s] = "term" /\ rh[s] # NoHash /\ \E e \in events : e.cid = s[1])
NeverConflict == ~(\E s \in Slots : pc[s] = "relook")
NeverUnacked == ~(\E s \in Slots : pc[s] = "unacked")
=============================================================================
