--------------------------- MODULE TicketSync ---------------------------
(***************************************************************************)
(* The ticket node's sync and pull (OMN-23761, plan task S4): two          *)
(* branches and a person act on ONE ticket, and Linear has no              *)
(* compare-and-swap.                                                       *)
(*                                                                         *)
(* WHAT IS MODELLED                                                        *)
(*  - The ticket (tk): blk, the fingerprint of the contract block in its   *)
(*    description; h, the hash of the live title and description as        *)
(*    Linear returns it; txt, TRUE while the text outside the block holds  *)
(*    a hand edit the block does not carry. A write by the node sets       *)
(*    h = <<"n", fingerprint>> (the node renders the whole description     *)
(*    from the file); a hand edit sets a fresh h.                          *)
(*  - Two branches, each with one file (files[b]): base, the fingerprint   *)
(*    recorded in the file by the last sync or pull; cur, the fingerprint  *)
(*    of the contract as the branch has edited it. A branch edit sets cur  *)
(*    to a fresh fingerprint.                                              *)
(*  - A person (HandEdit): edits the text outside the block (txt) or the   *)
(*    block itself, at any time, in any state. Linear takes it.            *)
(*  - The event log (ev): the written events and the pull adoption events, *)
(*    newest last. The first entry is the event the create (S3) published. *)
(*  - A command (slot) is one sync, sync --replace <fingerprint> or pull   *)
(*    on one branch; a branch runs its commands one after the other. A     *)
(*    sync is separate steps: Begin (read the ticket, the newest event and *)
(*    the file, then decide), Render (pure), Recheck (re-read the ticket), *)
(*    Write, [Readback], File (write the new fingerprint into the file),   *)
(*    Publish (the written event), Term (the one terminal). A pull is      *)
(*    Begin, PullFile, Publish, Term and writes no Linear. Any other       *)
(*    action (a hand edit, the other branch) may run between two steps.    *)
(*  - A run may crash between steps (Crash): memory is lost, Linear, the   *)
(*    files and the bus keep what they hold, and the command is delivered  *)
(*    again (at-least-once bus).                                           *)
(*                                                                         *)
(* THE DESIGN (what base.cfg checks; each knob below is one mutant)        *)
(*  Serial            one run at a time per ticket, from its read to its   *)
(*                    terminal (the command keyed by ticket id).           *)
(*  DriftCheck        sync refuses when the ticket's block fingerprint is  *)
(*                    not the file's recorded fingerprint (drift).         *)
(*  HashCheck         sync refuses when the live hash is not the newest    *)
(*                    event's hash (hand edit).                            *)
(*  PullFlagsText     a pull's adoption event says whether the ticket's    *)
(*                    text outside the block was carried into the file     *)
(*                    (cap); a sync refuses behind an event with cap       *)
(*                    false. (The brief's design is the event with the     *)
(*                    hash alone: mutant M5.)                              *)
(*  Reread            the narrowest fix for the read-then-write gap: the   *)
(*                    ticket is read again just before the write and the   *)
(*                    run ends refused ticket_changed when it moved.       *)
(*  HashFrom response the written event hashes what the write call's own   *)
(*                    response returned (mutant: a later readback).        *)
(*  ReplaceChecksName sync --replace X writes only when the ticket's block   *)
(*                    fingerprint is X.                                    *)
(*  NoopSkip          a sync whose file, ticket block and hash all agree   *)
(*                    makes no write and publishes no event.               *)
(*  FileWrite         a written sync records the new fingerprint in the    *)
(*                    file.                                                *)
(*  EmitOnRefuse      every refusal ends in a terminal.                    *)
(*  Recover           a sync delivered again after its run crashed finds    *)
(*                    the ticket exactly as the node's own write leaves    *)
(*                    it (block is the file's contract, hash is the        *)
(*                    render's) and the newest event not that hash, takes  *)
(*                    the write as its own and finishes: file, event,      *)
(*                    terminal. Without it the run refuses its own write.  *)
(*  Redeliver         a command whose run crashed is delivered again (the  *)
(*                    at-least-once bus); the mutant loses it.             *)
(*  EventKeyed        an event is keyed by its command, kind and hash: a   *)
(*                    command delivered again after a crash that already   *)
(*                    published its event publishes the same event, not a  *)
(*                    second one.                                          *)
(*                                                                         *)
(* PROPERTIES                                                              *)
(*  P1  OverwriteOnlyInGap (design): a node write that lands over a        *)
(*      change since its first read, or over a hand edit it was not given  *)
(*      leave to replace, does so only when a hand edit landed after that  *)
(*      run's re-read (the residual race).                                 *)
(*  P1s NoSilentOverwrite (strict): no node write lands over such a        *)
(*      change at all. residual.cfg checks it and TLC finds the residual.  *)
(*  P2  SyncedMatch: after a sync's terminal written, while the ticket is  *)
(*      as that sync wrote it, the ticket block is the file's recorded     *)
(*      fingerprint.                                                       *)
(*  P3  StaleRefused: a sync (not --replace) writes only when the ticket's *)
(*      block is the file's fingerprint, or a hand edit landed after the   *)
(*      re-read.                                                           *)
(*  P4  OneTerm, DoneHasTerm: every command ends in exactly one terminal.  *)
(*  P5  NoopNoWrite, EventsExact, EventsAtMostOne: the event counts and    *)
(*      hashes of plan AC1 and AC5.                                        *)
(*  P6  a sync is never refused because of the node's own earlier write    *)
(*      (a crash after the Linear write; the Recover knob).                *)
(*                                                                         *)
(* NOT MODELLED: time and Linear's updatedAt (a changed hash stands for a  *)
(* changed updatedAt; the model's change detector is exact, Linear's may   *)
(* over-refuse on a state move: that is a retry, not a loss); a hand edit  *)
(* put back so the hash repeats (the content is then what the node wrote); *)
(* the S3 create; a run kept alive past its lock (a zombie holder: with it *)
(* the Serial knob is off, M2); the event log lagging its publish; the     *)
(* content of the contract and of the render; a partition that loses a     *)
(* published terminal; a pull that overwrites a branch's unsynced file     *)
(* edit (a local, git-recoverable loss); more than two branches.           *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS Branches, Kinds, Cmds1, Cmds2, MaxHand, MaxFEdit, MaxCrash,
          Serial, DriftCheck, HashCheck, PullFlagsText, Reread,
          ReadbackHash, ReplaceChecksName, NoopSkip, FileWrite, EmitOnRefuse,
          EventKeyed, Redeliver, Recover

VARIABLES tk, files, r, terms, ev, W, hn, fe, crashes, lateH

vars == <<tk, files, r, terms, ev, W, hn, fe, crashes, lateH>>

Slots == {<<"b1", k>> : k \in 1..Cmds1} \cup {<<"b2", k>> : k \in 1..Cmds2}
Br(s) == s[1]
Idx(s) == s[2]

Fp0 == <<"fp", "init", 0>>
NoFp == <<"none", "none", "none">>
NoH == <<"none", "none">>
H0 == <<"n", Fp0>>
FileFps == {<<"fp", b, k>> : b \in Branches, k \in 1..MaxFEdit}
HandFps == {<<"fp", "hand", n>> : n \in 1..MaxHand}
Fps == {Fp0} \cup FileFps \cup HandFps

ActivePcs == {"read", "pullread", "ending", "rendered", "rechecked",
              "wrote", "written", "filed", "published"}
Active(s) == r[s].pc \in ActivePcs

Newest == ev[Len(ev)]

Init0 == [pc |-> "idle", kind |-> "sync", X |-> NoFp, rb |-> NoFp, rh |-> NoH,
          rt |-> FALSE, eh |-> NoH, ec |-> TRUE, cur |-> NoFp, base |-> NoFp,
          out |-> "none", wh |-> NoH, noopc |-> FALSE]

Init ==
  /\ tk = [blk |-> Fp0, h |-> H0, txt |-> FALSE, by |-> <<"create", 0>>]
  /\ files = [b \in Branches |-> [base |-> Fp0, cur |-> Fp0]]
  /\ r = [s \in Slots |-> Init0]
  /\ terms = [s \in Slots |-> <<>>]
  /\ ev = <<[h |-> H0, cap |-> TRUE, kind |-> "written", by |-> <<"create", 0>>]>>
  /\ W = {}
  /\ hn = 0
  /\ fe = [b \in Branches |-> 0]
  /\ crashes = 0
  /\ lateH = [s \in Slots |-> FALSE]

SetR(s, rec) == r' = [r EXCEPT ![s] = rec]

(* A person edits the ticket. A hand edit landing while a run waits between *)
(* its re-read and its write is the late hand edit (lateH).                 *)
HandEdit(kind) ==
  /\ hn < MaxHand
  /\ tk' = IF kind = "text"
             THEN [tk EXCEPT !.h = <<"h", hn + 1>>, !.txt = TRUE, !.by = <<"hand", 0>>]
             ELSE [blk |-> <<"fp", "hand", hn + 1>>, h |-> <<"h", hn + 1>>,
                   txt |-> tk.txt, by |-> <<"hand", 0>>]
  /\ hn' = hn + 1
  /\ lateH' = [s \in Slots |-> IF r[s].pc = "rechecked" THEN TRUE ELSE lateH[s]]
  /\ UNCHANGED <<files, r, terms, ev, W, fe, crashes>>

FileEdit(b) ==
  /\ fe[b] < MaxFEdit
  /\ files' = [files EXCEPT ![b].cur = <<"fp", b, fe[b] + 1>>]
  /\ fe' = [fe EXCEPT ![b] = fe[b] + 1]
  /\ UNCHANGED <<tk, r, terms, ev, W, hn, crashes, lateH>>

Issue(s) ==
  /\ r[s].pc = "idle"
  /\ (IF Idx(s) = 1 THEN TRUE ELSE r[<<Br(s), Idx(s) - 1>>].pc = "done")
  /\ \E k \in Kinds : \E x \in (IF k = "replace" THEN Fps ELSE {NoFp}) :
       SetR(s, [r[s] EXCEPT !.pc = "pending", !.kind = k, !.X = x])
  /\ UNCHANGED <<tk, files, terms, ev, W, hn, fe, crashes, lateH>>

(* Read the ticket, the newest event and the file; then decide. *)
Begin(s) ==
  /\ r[s].pc = "pending"
  /\ (~Serial \/ ~(\E t \in Slots : Active(t)))
  /\ LET rs == r[s]
         base == files[Br(s)].base
         cur == files[Br(s)].cur
         E == Newest
         noopc == cur = tk.blk /\ base = tk.blk /\ tk.h = E.h /\ E.cap /\ ~tk.txt
         resume == Recover /\ rs.kind # "pull" /\ tk.blk = cur
                   /\ tk.h = <<"n", cur>> /\ tk.h # E.h
         out ==
           IF rs.kind = "pull" THEN "pull"
           ELSE IF resume THEN "resume"
           ELSE IF rs.kind = "replace"
             THEN (IF ReplaceChecksName /\ tk.blk # rs.X THEN "mismatch"
                   ELSE IF NoopSkip /\ noopc THEN "noop"
                   ELSE "proceed")
             ELSE (IF DriftCheck /\ tk.blk # base THEN "drift"
                   ELSE IF HashCheck /\ (tk.h # E.h \/ (PullFlagsText /\ ~E.cap))
                     THEN "hand_edit"
                   ELSE IF NoopSkip /\ noopc THEN "noop"
                   ELSE "proceed")
     IN SetR(s, [rs EXCEPT !.pc = IF out = "proceed" THEN "read"
                                   ELSE IF out = "pull" THEN "pullread"
                                   ELSE IF out = "resume" THEN "written"
                                   ELSE "ending",
                           !.wh = tk.h,
                           !.rb = tk.blk, !.rh = tk.h, !.rt = tk.txt,
                           !.eh = E.h, !.ec = E.cap, !.cur = cur, !.base = base,
                           !.out = out, !.noopc = noopc])
  /\ lateH' = [lateH EXCEPT ![s] = FALSE]
  /\ UNCHANGED <<tk, files, terms, ev, W, hn, fe, crashes>>

Render(s) ==
  /\ r[s].pc = "read"
  /\ SetR(s, [r[s] EXCEPT !.pc = "rendered"])
  /\ UNCHANGED <<tk, files, terms, ev, W, hn, fe, crashes, lateH>>

(* The narrowest fix: read the ticket again just before the write. *)
Recheck(s) ==
  /\ r[s].pc = "rendered"
  /\ IF Reread /\ (tk.h # r[s].rh \/ tk.blk # r[s].rb)
       THEN SetR(s, [r[s] EXCEPT !.pc = "ending", !.out = "changed"])
       ELSE SetR(s, [r[s] EXCEPT !.pc = "rechecked"])
  /\ UNCHANGED <<tk, files, terms, ev, W, hn, fe, crashes, lateH>>

Write(s) ==
  /\ r[s].pc = "rechecked"
  /\ LET rs == r[s]
         b == Br(s)
         chg == tk.h # rs.rh
         l2 == rs.kind # "replace" /\ (tk.txt \/ tk.blk # files[b].base)
         l3 == rs.kind = "replace" /\ tk.blk # rs.X
         stale == rs.kind # "replace" /\ tk.blk # files[b].base
         hash == <<"n", rs.cur>>
     IN /\ W' = W \cup {[s |-> s, lost |-> (chg \/ l2 \/ l3), late |-> lateH[s],
                         stale |-> stale, noop |-> rs.noopc,
                         rep |-> (rs.kind = "replace"), rt |-> rs.rt]}
        /\ tk' = [blk |-> rs.cur, h |-> hash, txt |-> FALSE, by |-> s]
        /\ SetR(s, [rs EXCEPT !.wh = IF ReadbackHash THEN NoH ELSE hash,
                              !.pc = IF ReadbackHash THEN "wrote" ELSE "written"])
  /\ UNCHANGED <<files, terms, ev, hn, fe, crashes, lateH>>

(* The mutant: the hash of the written ticket is read in a later call. *)
Readback(s) ==
  /\ r[s].pc = "wrote"
  /\ SetR(s, [r[s] EXCEPT !.wh = tk.h, !.pc = "written"])
  /\ UNCHANGED <<tk, files, terms, ev, W, hn, fe, crashes, lateH>>

File(s) ==
  /\ r[s].pc = "written"
  /\ files' = IF FileWrite THEN [files EXCEPT ![Br(s)].base = r[s].cur] ELSE files
  /\ SetR(s, [r[s] EXCEPT !.pc = "filed"])
  /\ UNCHANGED <<tk, terms, ev, W, hn, fe, crashes, lateH>>

PullFile(s) ==
  /\ r[s].pc = "pullread"
  /\ files' = [files EXCEPT ![Br(s)] = [base |-> r[s].rb, cur |-> r[s].rb]]
  /\ SetR(s, [r[s] EXCEPT !.pc = "filed"])
  /\ UNCHANGED <<tk, terms, ev, W, hn, fe, crashes, lateH>>

Publish(s) ==
  /\ r[s].pc = "filed"
  /\ LET rs == r[s]
         pull == rs.kind = "pull"
         rec == [h |-> IF pull THEN rs.rh ELSE rs.wh,
                 cap |-> IF pull THEN (IF PullFlagsText THEN ~rs.rt ELSE TRUE)
                         ELSE TRUE,
                 kind |-> IF pull THEN "adoption" ELSE "written",
                 by |-> s]
         seen == \E i \in 1..Len(ev) :
                   ev[i].by = s /\ ev[i].kind = rec.kind /\ ev[i].h = rec.h
     IN ev' = IF EventKeyed /\ seen THEN ev ELSE Append(ev, rec)
  /\ SetR(s, [r[s] EXCEPT !.pc = "published"])
  /\ UNCHANGED <<tk, files, terms, W, hn, fe, crashes, lateH>>

Term(s) ==
  /\ r[s].pc \in {"ending", "published"}
  /\ LET rs == r[s]
         t == IF rs.pc = "ending" THEN rs.out
              ELSE IF rs.kind = "pull" THEN "pulled" ELSE "written"
     IN terms' = IF rs.pc = "ending" /\ ~EmitOnRefuse THEN terms
                 ELSE [terms EXCEPT ![s] = Append(terms[s], t)]
  /\ SetR(s, [r[s] EXCEPT !.pc = "done"])
  /\ UNCHANGED <<tk, files, ev, W, hn, fe, crashes, lateH>>

Crash(s) ==
  /\ crashes < MaxCrash
  /\ Active(s)
  /\ SetR(s, [Init0 EXCEPT !.pc = IF Redeliver THEN "pending" ELSE "lost", !.kind = r[s].kind, !.X = r[s].X])
  /\ lateH' = [lateH EXCEPT ![s] = FALSE]
  /\ crashes' = crashes + 1
  /\ UNCHANGED <<tk, files, terms, ev, W, hn, fe>>

Terminated ==
  /\ \A s \in Slots : r[s].pc = "done"
  /\ UNCHANGED vars

Next ==
  \/ \E k \in {"text", "block"} : HandEdit(k)
  \/ \E b \in Branches : FileEdit(b)
  \/ \E s \in Slots : Issue(s) \/ Begin(s) \/ Render(s) \/ Recheck(s)
                      \/ Write(s) \/ Readback(s) \/ File(s) \/ PullFile(s)
                      \/ Publish(s) \/ Term(s) \/ Crash(s)
  \/ Terminated

Spec == Init /\ [][Next]_vars

(* ---------------------------- invariants ---------------------------- *)
TypeOK ==
  /\ tk.blk \in Fps
  /\ \A b \in Branches : files[b].base \in Fps /\ files[b].cur \in Fps
  /\ \A s \in Slots : Len(terms[s]) <= 1

OneTerm == \A s \in Slots : Len(terms[s]) <= 1

DoneHasTerm == \A s \in Slots : r[s].pc = "done" => Len(terms[s]) = 1

P1 == \A w \in W : w.lost => w.late
P1s == \A w \in W : ~w.lost
P3 == \A w \in W : w.stale => w.late
NoopNoWrite == \A w \in W : ~w.noop

(* A sync is never refused because of the node's own earlier write: when it *)
(* reads the ticket exactly as that write leaves it (block is the file's    *)
(* contract, hash is the render's) and the newest event is not that hash,  *)
(* it does not end drift, hand edit or mismatch.                            *)
P6 ==
  \A s \in Slots :
    (r[s].pc \in {"ending", "done"} /\ r[s].kind # "pull"
       /\ r[s].out \in {"drift", "hand_edit", "mismatch"})
      => ~(r[s].rb = r[s].cur /\ r[s].rh = <<"n", r[s].cur>> /\ r[s].rh # r[s].eh)

SyncedMatch ==
  \A s \in Slots :
    (Len(terms[s]) = 1 /\ terms[s][1] = "written" /\ tk.by = s)
      => tk.blk = files[Br(s)].base

EvCount(s) == Cardinality({i \in 1..Len(ev) : ev[i].by = s})

EventsAtMostOne ==
  \A s \in Slots : \A i, j \in 1..Len(ev) :
    (i # j /\ ev[i].by = s /\ ev[j].by = s) => (ev[i].kind # ev[j].kind \/ ev[i].h # ev[j].h)

(* With no crash in the run's past: a written sync leaves exactly one      *)
(* written event carrying the hash of what it wrote; a pull exactly one    *)
(* adoption event carrying the hash as read; every other end, none.        *)
EventsExact ==
  (crashes = 0) =>
    \A s \in Slots :
      (r[s].pc = "done" /\ Len(terms[s]) = 1) =>
        LET t == terms[s][1]
            evs == {i \in 1..Len(ev) : ev[i].by = s}
        IN IF t = "written"
             THEN /\ Cardinality(evs) = 1
                  /\ \A i \in evs : /\ ev[i].kind = "written"
                                    /\ ev[i].h = <<"n", r[s].cur>>
             ELSE IF t = "pulled"
               THEN /\ Cardinality(evs) = 1
                    /\ \A i \in evs : /\ ev[i].kind = "adoption"
                                      /\ ev[i].h = r[s].rh
               ELSE Cardinality(evs) = 0

(* ---------------------------- witnesses ----------------------------- *)
(* Each is a "never reached" invariant that TLC must report violated, so   *)
(* the passing runs are not vacuous.                                       *)
Ended(t) == \E s \in Slots : Len(terms[s]) = 1 /\ terms[s][1] = t
NeverWritten == ~Ended("written")
NeverPulled == ~Ended("pulled")
NeverNoop == ~Ended("noop")
NeverDrift == ~Ended("drift")
NeverHandEdit == ~Ended("hand_edit")
NeverChanged == ~Ended("changed")
NeverMismatch == ~Ended("mismatch")
NeverReplaceOverText == \A w \in W : ~(w.rep /\ w.rt)
NeverUncapturedAdoption == \A i \in 1..Len(ev) : ev[i].cap
NeverBothBranchesWrote ==
  ~(\E s, t \in Slots : Br(s) # Br(t) /\ Len(terms[s]) = 1 /\ terms[s][1] = "written"
                        /\ Len(terms[t]) = 1 /\ terms[t][1] = "written")
NeverResumed == \A s \in Slots : r[s].out # "resume"
NeverCrashedThenEnded ==
  ~(crashes > 0 /\ \E s \in Slots : r[s].pc = "done")

==========================================================================
