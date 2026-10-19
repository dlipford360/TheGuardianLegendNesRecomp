# Unexpected return during the first-level transition

The production fault log records repeated `game_run_main returned unexpectedly`
events with bank 3, guest PC `$9380`, and stack pointer `$FD`. These faults are
written to `build/game/runtime_faults.log`, even when the terminal log is quiet.

Generated bank-3 code for `$937E` previously dispatched its backward branch to
`$9371`, despite having already emitted that label in the same C function.
The fallback interpreter entered with an outer saved value still on the stack.
After finishing the inner loop, the PLA at `$9380` lifted the stack above that
new interpreter boundary, returning prematurely before the surrounding routine
could finish. This matches the recorded fault state.

The generator now recognizes already-emitted instruction labels as valid local
backward-branch targets. The conservative pre-scan can miss them after inline
trampoline operands reached through a JMP thunk. No watchdog limit or stack
escape guard was relaxed.

Validation:

- A synthetic thunk/inline-data/stack-preserving-loop regression fails against
  a compiler with the change removed and passes with the change. Negative-control
  artifacts: `build/completion-negative/`.
- All 56 code-generation tests pass.
- UxROM fixture: 477 checks plus RMW bus-order check pass.
- Sound-stack fixture: 44 checks per variant pass.
- Mapper and raster CTests pass.
- Windowed combat replay completes through frame 3575, with audio active and a
  visible corridor in the final screenshot:
  `build/first-level-runs/combat-lnxtx_yi/`.

Full boss defeat and transition have not been replayed successfully here. An
extended pre-fix input replay ran to frame 16775 but ended on a black screen;
that run is not evidence of successful level completion. The compiler regression
and the fault-state match support this fix, but a new successful gameplay
transition is still needed to confirm the reported crash is fully resolved.

## Player follow-up

The player subsequently confirmed successful first-level completion. A later
watchdog reported entry `$9A12`, with the current bank already changed to 3.
Bank 6 has a routine at that entry calling bank5:$8012 through the inline bank
trampoline. Both entries are now explicit native-generation roots in `game.toml`.
The rebuilt dispatch table covers both, and the windowed combat replay passed
(`build/first-level-runs/combat-wng2x3e0/`). The exact post-level interaction has
not yet been reproduced locally, so this follow-up change remains a candidate
fix pending gameplay confirmation.

The player confirmed extended play after that change, then reported a watchdog
at fixed-bank entry `$F5D9` after completing the second level (frame 52290).
This is executable code immediately following inline `YOU GOT KEY` text, and
was absent from native dispatch. It is now an explicit fixed-bank generation
root. The rebuilt dispatch table calls `func_F5D9`, and windowed combat passes
(`build/first-level-runs/combat-l7pcvns3/`). The second-level reward sequence
still needs gameplay confirmation; the combat replay does not reach it.

## Black screen after the final death

After the last life ended, the GAME OVER screen appeared but the title
transition never completed: the display stayed off and the frame hash froze on
a black screen. The death handler exits by design through `$8260 JSR $C0ED`
then `$8263 JMP $C14D`, which re-enters the main loop at `$FF4A` (`LDX #$FF;
TXS`) so the `$FF4D-$FF77` re-init (display control, `$D852 #$1F`, `$EDD1`
clear, bank calls) runs before the task loop resumes.

Two dynamically-entered addresses in that tail were missing from native
generation: the `$822D JSR $C06C` return-adjust follow enters `$8234` (string
output), and the `$C06F` queue trampoline enqueues the `$8243` wait-loop
continuation, which the queue executor dispatches as a task entry. Interpreted,
the tail flowed `$8263 → $C14D → $FF4A → $FF4C`, where `TXS` lifted S above the
interpreter's entry frame; the run exited through the stack-escape path, which
discards the `$FF4D` continuation, and the bail cascade unwound to the reset
handler's inlined copy of the main loop. There the `$FF7F JSR $FF8B` stack check
passed spuriously (S=`$FF` equals the loop's own entry stack), so the loop
resumed mid-body at `$FF82` — the re-init never ran and the display stayed off.
`$8234` and `$8243` are now bank-0 native-generation roots in `game.toml`;
compiled, the `JMP $C14D → $FF4A` re-entry runs the full init like hardware.

Title-idle verification of that fix exposed a second, pre-existing hang: the
attract demo never started (black from frame ~1600 while the oracle showed demo
gameplay). The transition's clear routine ends with a `$2002` vblank poll
(`$EDF9 LDA $2002; $EDFC BPL $EDF9`), and the game's NMI handler reads `$2002`
twice, consuming the flag. Recompiled NMI runs atomically inside the frame
boundary call, so the handler always consumed the flag before the game's poll
could observe it; on hardware the tight poll wins the race window between the
flag being set and the handler's first read. `nes_vblank_callback()` now re-arms
bit 7 after the top-level NMI handler returns (a no-op when the handler never
read `$2002`).

Validation:

- Scripted boss-death retry: GAME OVER at frame 4900, title at 5000, START
  resumes a new game at 5600; zero dispatch misses, no watchdog or faults
  (`build/boss-death-runs/fixcheck4/`).
- Title idle 10k frames: the attract demo now cycles title → demo → title,
  matching the oracle's demo content at frames 2500/5000 where native was
  previously black (`build/title-idle-10k-r3/`).
- Windowed combat replay passes (`build/first-level-runs/combat-mue4ctfh/`).
- 74 code-generation tests, UxROM fixture 477 checks, sound-stack fixture
  44+44 checks, both project CTests, and the interpreter (77 checks),
  runtime-boundary, and mapper self-tests all pass.

## Dead retry prompt after answering a death (save-state repro)

A player save state from a deep stage (`Shift+F1`, slot 9) reproduced a new
failure: after dying and pressing START at the retry prompt, START did nothing,
SELECT opened the item menu, and the terminal showed

```
[interp] WATCHDOG: run from $829C exceeded 2000000 instrs in one call (bank=0, ipc=$DAD8) — bailing
[runtime] interp declined dispatch miss at $829C ... Snapshotting and pausing instead of exiting.
```

`$829C` is the retry prompt's wait-for-START handler (`JSR $82B0; JSR $C0E4;
AND #$10; BEQ $829F`, then `$82AA JSR $C0ED; $82AD JMP $C150` into the `$FF4A`
re-init family). The queue dispatcher enters it dynamically, and it was not a
native root. Interpreted, the `$829F` poll loop runs inside one atomic
interpreter call: input is never re-latched mid-call, so a held START is
invisible, the loop spins to the 2M-instruction watchdog, and the runtime
pauses with a dead keyboard. The SELECT-menu symptom was bail-cascade fallout
from the same stall. `0x829C` is now a bank-0 native root; compiled, the poll
loop crosses frame boundaries, the START latch updates, and the re-init resumes
play like hardware.

Validation (all on the player's slot-9 state, headless):

- Die → wait 30s → START: continue proceeds (password-record prompt, progress
  kept: SC 312900, CHIP 150), zero watchdogs (`build/rt/slot9-die2.script`).
- Die → SELECT on the continue screen: no item menu, screen unchanged
  (`build/rt/slot9-select.script`).
- `--shoot`, `--combat`, and early first-level harness scenarios pass; 74
  code-generation tests pass.

## Intermittent watchdog at the sound stub walker after retry

A follow-up report on the same die → wait → START flow showed a different
dynamic entry, intermittently:

```
[interp] WATCHDOG: run from $D5EA exceeded 2000000 instrs in one call (bank=0, ipc=$DADA) — bailing
```

`$D5EA` is the sound stub-list walker's "advance to next stub" continuation:
`$D58A` jumps through the `($16)` cursor (stack-RTS indirect), and the walker
re-enters at `$D5EA` after each stub. When the retry flow dispatches the
walker while the APU queue is busy, the interpreted run reaches the `$DAD8`
`$2B` drain wait (`ipc=$DADA` is the PC at bail time); `$2B` only clears at
frame boundaries, which the atomic interpreter never reaches, so it spins to
the watchdog and pauses — the same dead-retry symptom, gated on sound state,
which is why it only fired sometimes. `0xD5EA` is now a fixed-bank native
root; dispatch resolves it natively and the drain loop crosses frame
boundaries like hardware.

Validation: nine die → wait → START variants on the player's slot-9 state
(including firing-sound-heavy deaths and four wait timings) run with zero
watchdogs and zero declines; `--shoot`, `--combat`, and early scenarios and
the 74 code-generation tests pass. The intermittent trigger did not reproduce
headlessly pre-fix, so the mechanism (entry `$D5EA`, bail PC inside the `$2B`
drain wait) plus the native dispatch entry carry the diagnosis; player
play-test is the final confirmation.

## Intermittent watchdog in the death handler's stub window ($8238)

A further report on the same flow, with a repeatable recipe: load slot 9,
die, wait ~25 s, press START, reload slot 9, die again — the second death
could bail with

```
[interp] WATCHDOG: run from $8238 exceeded 2000000 instrs in one call (bank=0, ipc=$DAD8) — bailing
```

`$8238` sits inside the death handler's inline sound-stub window
(`$8236-$8242`). The `$8234` `JSR $C06F` (`= JMP $D576`) pops the return
address (`$8236`) into the walker cursor `$16/$17`; `$D58A` dispatches each
stub with `PHA/PHA/RTS`, which executes at `($16)+1` — so odd cursor values
run at even addresses (`$8238`, `$823A`, ...). Those addresses are
mid-instruction for the static decode of the `$8237` data bytes, so a walker
jump there was a dispatch miss; interpreted, the run again reached the `$2B`
APU-drain wait and spun to the watchdog, gated on queue state — hence the
second-death intermittency.

Fix: the whole stub window (`0x8236, 0x8238, 0x823A, 0x823C, 0x823E, 0x8240,
0x8242`) is now bank-0 native roots alongside the existing `$8234`/`$8243`
entries. The recompiler re-decodes each mid-instruction entry and merges
them as multi-entry cases (`func_8236_b0_body` covers `$8236-$823C`,
`func_8240_b0_body` covers `$8240-$8243`), so every stub the walker can
dispatch runs natively and the drain loop crosses frame boundaries.

Validation: the two-load die → wait → START recipe replays byte-identically
(all 22 frame hashes match the pre-fix interpreted run — the native stub
decode matches the interpreter), with zero watchdogs; `--shoot`, `--combat`,
and early scenarios and the 74 code-generation tests pass; the windowed
F9-hotkey double-load recipe is clean. As with `$D5EA`, the exact
queue-busy timing did not reproduce headlessly pre-fix, so the mechanism
plus native dispatch carry the diagnosis; player play-test is final
confirmation.

## Black screen after reload: ghost tail-call slots (root cause of the
## reload-continuation bug class)

The `$8238`/`$D5EA` reports shared one trigger: they only fired on the
*second* die-after-a-save-state-reload. With the stub window rooted, the
symptom moved to a black screen: the retry walker dispatched correctly but
execution landed at `$8285` — mid-string in the CONTINUE/PUSH START prompt —
whose decode runs into `JSR $5020` (open bus → BRK).

`TGL_DERAIL_TRACE=1` (runtime ring of recent boundaries: pc, S, A, bank,
`$16` cursor, stack-page bytes, plus a write-watch on `$01F0-$01FF`) captured
the exact moment: the walker's `$D58A` `PHA/PHA/RTS` dispatch computed the
correct target `$829C`, but the CPU executed `$8285` instead.

Root cause: `call_by_address_tail`'s tail-slot table (`s_tail_active[]`)
mirrors live C-stack frames to flatten JMP loops. A save-state load escapes
guest execution via `longjmp`, discarding every native frame — but
`runtime_prepare_guest_resume` cleared only `s_tail_pending`/`s_tail_caller`
and left `s_tail_active_n`/`s_tail_pending_slot` populated. Ghost slots then
claim ancestors that no longer exist: a tail dispatch to an address that was
tail-active before the reload (e.g. the `$829C` retry handler, live when the
hotkey fires) defers to the ghost — the handler silently never runs — and the
deferred lap is later consumed by the wrong frame, which re-dispatches *its
own* address. That is how `$829C` became `$8285`.

Fix: the resume path now drops the whole tail-slot table. The earlier
dynamic-entry roots (`$8234` family, `$829C`, `$D5EA`, stub window) remain
correct and necessary — interpreter islands are atomic — but the ghost slots
were the reason those paths got exercised in corrupted states after reloads.

Validation: player repeated the load-9 → die → wait → START → reload → die
recipe several times with zero black screens, zero watchdogs, zero BRKs;
`--shoot` and the double-load cosim replay (byte-identical hashes) pass.
