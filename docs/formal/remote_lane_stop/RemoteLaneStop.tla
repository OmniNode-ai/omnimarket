---------------------------- MODULE RemoteLaneStop ----------------------------
(* Stopping a running remote lane by name: the race of the lane's two possible
   closers and the bounded escalation from the polite signal to the forceful one.

   One lane has one engine process tree on its host and one CLAIM on the rolling
   ledger. Who may close it:
     the stop node   writes TERMINAL outcome=stopped, only after the process tree
                     is gone, and only for a lane it found open
     the runner      the lane's own closeout, when its process is still alive and
                     sees the engine end for any reason, writes its own TERMINAL
   The ledger accepts one TERMINAL per closed CLAIM (single closer): a second
   closer is refused, and that refusal makes the loser a no-op. The stop node that
   loses reports `already_ended` and writes nothing else.

   Stoppers s1 and s2 are two stop commands for the same lane (an operator that
   asks twice, or the orchestrator and a lane). The engine may ignore the polite
   signal (ignoresTerm) and may not die even of the forceful one (unkillable, a
   process stuck in the kernel). The runner may be dead (runnerAlive = FALSE), in
   which case only a stopper can close the lane. The lane may already have ended
   when the stop arrives, with or without its TERMINAL.

   Time is the stopper's own wait counter: the polite signal, up to PoliteWait
   polls, then the forceful signal, up to KillWait polls, then a typed `refused`
   that names the survivor. The engine, the runner and the other stopper move at
   any rate between the polls.

   Properties (each is broken by its own mutation below):
     AtMostOneTerminal       one TERMINAL per CLAIM, whoever the writers are
     TerminalAfterExit       a TERMINAL exists only once the process tree is gone
     StoppedIsOwnWrite       outcome stopped means this stopper's row is the one
     LoserWritesNothing      outcome already_ended means this stopper wrote none
     RefusedWritesNothing    a refusal (survivor) leaves no row
     EndedHasTerminal        stopped or already_ended is only claimed with a row
     NoSignalWhenAlreadyEnded a stop that read an ended lane sent no signal
     KillAfterWait           the forceful signal follows the polite one and a
                             full polite wait
     EveryStopAnswered       every stop command ends in a typed outcome (liveness)
   Mutations (cfg constants) that must break them:
     SingleCloser=FALSE   the ledger takes every TERMINAL     -> AtMostOneTerminal
     WaitForExit=FALSE    the TERMINAL follows the signal     -> TerminalAfterExit
     WaitBeforeKill=FALSE the forceful signal skips the wait  -> KillAfterWait
     CheckFirst=FALSE     the signal is sent before the read  -> NoSignalWhenAlreadyEnded
     GiveUp=FALSE         no bound on the forceful wait       -> EveryStopAnswered *)
EXTENDS Integers, FiniteSets

CONSTANTS PoliteWait, KillWait,
          SingleCloser, WaitForExit, WaitBeforeKill, CheckFirst, GiveUp

Stoppers == {"s1", "s2"}

VARIABLES engine,       \* "running" | "exited": the lane's process tree
          ignoresTerm,  \* the engine survives the polite signal
          unkillable,   \* the engine survives the forceful signal
          runnerAlive,  \* the lane's own runner still follows it
          runnerDone,   \* the runner has made its one attempt to close
          sigs,         \* signals delivered to the process tree
          ledger,       \* TERMINAL rows accepted for the lane's CLAIM: <<writer, kind>>
          pc, clock, out, via, sent, forcedAt

vars == <<engine, ignoresTerm, unkillable, runnerAlive, runnerDone, sigs, ledger,
          pc, clock, out, via, sent, forcedAt>>

HasTerminal == ledger # {}

\* The ledger append. With the single-closer guard a second TERMINAL is refused.
Appended(rec) == IF SingleCloser /\ HasTerminal THEN ledger ELSE ledger \cup {rec}

Init ==
  /\ ignoresTerm \in BOOLEAN
  /\ unkillable \in BOOLEAN
  /\ runnerAlive \in BOOLEAN
  /\ runnerDone = FALSE
  /\ \/ engine = "running" /\ ledger = {}
     \/ engine = "exited" /\ ledger = {}
     \/ engine = "exited" /\ ledger = {<<"runner", "runner">>}
  /\ sigs = {}
  /\ pc = [s \in Stoppers |-> "idle"]
  /\ clock = [s \in Stoppers |-> 0]
  /\ out = [s \in Stoppers |-> "none"]
  /\ via = [s \in Stoppers |-> "none"]
  /\ sent = [s \in Stoppers |-> FALSE]
  /\ forcedAt = [s \in Stoppers |-> -1]

\* ---- the engine and the runner ------------------------------------------
EngineExits ==
  /\ engine = "running" /\ engine' = "exited"
  /\ UNCHANGED <<ignoresTerm, unkillable, runnerAlive, runnerDone, sigs, ledger,
                 pc, clock, out, via, sent, forcedAt>>

EngineTerm ==
  /\ engine = "running" /\ "TERM" \in sigs /\ ~ignoresTerm /\ engine' = "exited"
  /\ UNCHANGED <<ignoresTerm, unkillable, runnerAlive, runnerDone, sigs, ledger,
                 pc, clock, out, via, sent, forcedAt>>

EngineKill ==
  /\ engine = "running" /\ "KILL" \in sigs /\ ~unkillable /\ engine' = "exited"
  /\ UNCHANGED <<ignoresTerm, unkillable, runnerAlive, runnerDone, sigs, ledger,
                 pc, clock, out, via, sent, forcedAt>>

RunnerCloseout ==
  /\ runnerAlive /\ ~runnerDone /\ engine = "exited"
  /\ ledger' = Appended(<<"runner", "runner">>)
  /\ runnerDone' = TRUE
  /\ UNCHANGED <<engine, ignoresTerm, unkillable, runnerAlive, sigs,
                 pc, clock, out, via, sent, forcedAt>>

\* ---- one stop command -----------------------------------------------------
EndedRead(s) ==  \* the stopper reads the lane as ended: typed no-op
  /\ pc' = [pc EXCEPT ![s] = "done"]
  /\ out' = [out EXCEPT ![s] = "already_ended"]
  /\ via' = [via EXCEPT ![s] = "read"]

Read(s) ==
  /\ pc[s] = "idle"
  /\ IF CheckFirst
       THEN IF HasTerminal THEN EndedRead(s) /\ UNCHANGED <<clock, sent>>
            ELSE pc' = [pc EXCEPT ![s] = "polite"] /\ UNCHANGED <<clock, sent, out, via>>
       ELSE pc' = [pc EXCEPT ![s] = "polite"] /\ UNCHANGED <<clock, sent, out, via>>
  /\ UNCHANGED <<engine, ignoresTerm, unkillable, runnerAlive, runnerDone, sigs,
                 ledger, forcedAt>>

Polite(s) ==
  /\ pc[s] = "polite"
  /\ sigs' = sigs \cup {"TERM"}
  /\ sent' = [sent EXCEPT ![s] = TRUE]
  /\ clock' = [clock EXCEPT ![s] = 0]
  /\ pc' = [pc EXCEPT ![s] = IF ~CheckFirst THEN "read2"
                              ELSE IF WaitForExit THEN "wait1" ELSE "write"]
  /\ UNCHANGED <<engine, ignoresTerm, unkillable, runnerAlive, runnerDone, ledger,
                 out, via, forcedAt>>

Read2(s) ==  \* only when CheckFirst = FALSE: the read comes after the signal
  /\ pc[s] = "read2"
  /\ IF HasTerminal THEN EndedRead(s)
     ELSE pc' = [pc EXCEPT ![s] = IF WaitForExit THEN "wait1" ELSE "write"]
          /\ UNCHANGED <<out, via>>
  /\ UNCHANGED <<engine, ignoresTerm, unkillable, runnerAlive, runnerDone, sigs,
                 ledger, clock, sent, forcedAt>>

Force(s) ==
  /\ sigs' = sigs \cup {"KILL"}
  /\ forcedAt' = [forcedAt EXCEPT ![s] = clock[s]]
  /\ clock' = [clock EXCEPT ![s] = 0]
  /\ pc' = [pc EXCEPT ![s] = "wait2"]

Wait1(s) ==
  /\ pc[s] = "wait1"
  /\ \/ /\ engine = "exited"
        /\ pc' = [pc EXCEPT ![s] = "write"]
        /\ UNCHANGED <<sigs, clock, forcedAt>>
     \/ /\ engine = "running" /\ clock[s] < PoliteWait
        /\ clock' = [clock EXCEPT ![s] = @ + 1]
        /\ UNCHANGED <<sigs, pc, forcedAt>>
     \/ /\ engine = "running" /\ clock[s] >= PoliteWait
        /\ Force(s)
     \/ /\ ~WaitBeforeKill /\ engine = "running" /\ clock[s] < PoliteWait
        /\ Force(s)
  /\ UNCHANGED <<engine, ignoresTerm, unkillable, runnerAlive, runnerDone, ledger,
                 out, via, sent>>

Wait2(s) ==
  /\ pc[s] = "wait2"
  /\ \/ /\ engine = "exited"
        /\ pc' = [pc EXCEPT ![s] = "write"]
        /\ UNCHANGED <<clock, out>>
     \/ /\ engine = "running" /\ clock[s] < KillWait
        /\ clock' = [clock EXCEPT ![s] = @ + 1]
        /\ UNCHANGED <<pc, out>>
     \/ /\ engine = "running" /\ clock[s] >= KillWait /\ GiveUp
        /\ pc' = [pc EXCEPT ![s] = "done"]
        /\ out' = [out EXCEPT ![s] = "refused"]
        /\ UNCHANGED clock
  /\ UNCHANGED <<engine, ignoresTerm, unkillable, runnerAlive, runnerDone, sigs,
                 ledger, via, sent, forcedAt>>

Write(s) ==
  /\ pc[s] = "write"
  /\ pc' = [pc EXCEPT ![s] = "done"]
  /\ IF SingleCloser /\ HasTerminal
       THEN /\ out' = [out EXCEPT ![s] = "already_ended"]
            /\ via' = [via EXCEPT ![s] = "cas"]
            /\ UNCHANGED ledger
       ELSE /\ out' = [out EXCEPT ![s] = "stopped"]
            /\ ledger' = ledger \cup {<<s, "stopped">>}
            /\ UNCHANGED via
  /\ UNCHANGED <<engine, ignoresTerm, unkillable, runnerAlive, runnerDone, sigs,
                 clock, sent, forcedAt>>

Stopper(s) == Read(s) \/ Polite(s) \/ Read2(s) \/ Wait1(s) \/ Wait2(s) \/ Write(s)

Next == EngineExits \/ EngineTerm \/ EngineKill \/ RunnerCloseout
        \/ \E s \in Stoppers : Stopper(s)

Fairness == /\ \A s \in Stoppers : WF_vars(Stopper(s))
            /\ WF_vars(EngineTerm) /\ WF_vars(EngineKill) /\ WF_vars(RunnerCloseout)

Spec == Init /\ [][Next]_vars /\ Fairness

\* ---- properties -----------------------------------------------------------
TypeOK ==
  /\ engine \in {"running", "exited"}
  /\ sigs \subseteq {"TERM", "KILL"}
  /\ \A s \in Stoppers : out[s] \in {"none", "stopped", "already_ended", "refused"}

AtMostOneTerminal == Cardinality(ledger) <= 1

TerminalAfterExit == HasTerminal => engine = "exited"

StoppedIsOwnWrite == \A s \in Stoppers : out[s] = "stopped" => <<s, "stopped">> \in ledger

LoserWritesNothing ==
  \A s \in Stoppers : out[s] = "already_ended" => <<s, "stopped">> \notin ledger

RefusedWritesNothing ==
  \A s \in Stoppers : out[s] = "refused" => <<s, "stopped">> \notin ledger

EndedHasTerminal ==
  \A s \in Stoppers : out[s] \in {"stopped", "already_ended"} => HasTerminal

NoSignalWhenAlreadyEnded ==
  \A s \in Stoppers : (out[s] = "already_ended" /\ via[s] = "read") => ~sent[s]

KillAfterWait ==
  /\ "KILL" \in sigs => "TERM" \in sigs
  /\ \A s \in Stoppers : forcedAt[s] # -1 => forcedAt[s] >= PoliteWait

EveryStopAnswered == \A s \in Stoppers : <>(out[s] # "none")

\* ---- witnesses: each invariant is violated, which shows the state is reachable
NeverStopped == \A s \in Stoppers : out[s] # "stopped"
NeverLoser == \A s \in Stoppers : ~(out[s] = "already_ended" /\ via[s] = "cas")
NeverEscalatedAndStopped == \A s \in Stoppers : ~(out[s] = "stopped" /\ forcedAt[s] # -1)
NeverRefused == \A s \in Stoppers : out[s] # "refused"
NeverReadEnded == \A s \in Stoppers : ~(out[s] = "already_ended" /\ via[s] = "read")
NeverRunnerWonOverStopper ==
  ~(\E s \in Stoppers : sent[s] /\ <<"runner", "runner">> \in ledger)
================================================================================
