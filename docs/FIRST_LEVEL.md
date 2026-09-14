# First-Level Sound-Stack Fix

Later update: the missing bottom status bar seen in these original captures is
now corrected separately; see [HUD.md](HUD.md). The evidence below records the
sound-stack fix before that renderer correction.

## Report And Scope

Reported: press Enter twice to start the first level, then the screen turns
black; log includes `BRK at $9F59 bank=0 (frame 1695)` and a macOS IMK Mach-port
message. `$9F59` is a zero byte in sound data, not a valid new code entry.
The IMK message alone does not establish a game-code failure and was not patched.

The exact frame-1695 BRK was not reproduced by the scripted runs. They did
reproduce a definite sound-stack corruption, both in the opening corridor and
in a delayed start after title music loops. The fix below eliminates that
corruption. It does not certify every possible cause of a black screen or imply
that sound/presentation now matches the reference emulator.

## Root Cause And Change

`$91F3` calls sound decoder `$92F7`. Its computed-RTS dispatch pushes a synthetic
return to `$9303` plus a handler address from the table at `$932C/$932D`.
Command `$82` enters `$9376`, whose four PLAs discard both that synthetic return
and the decoder caller's JSR operand. It then jumps to `$9239`, inside the outer
channel-update loop. It must not return to `$91F6`.

The decoder-only interpreter island uses the decoder's entry stack as its
return boundary. After the third PLA it sees a stack escape and returns with
the fourth PLA still unexecuted (`next_pc=$937E`). The RTS sidecar is still
`$9375`, an old dispatch operand, not the intended continuation. Native stack
checks then unwind the sound call and NMI without completing their restoration.

`game.toml` now forces interpretation from the enclosing sound-update entry
`bank0:$917C`, in addition to the existing entries. All four pops and the JMP
remain inside one architectural call frame; the eventual RTS returns normally
to NMI continuation `$DCE2`. Reset and non-sound game code remain native.
No BRK suppression, ROM modification, stack clamping, or game-data-as-code entry
was added. No new engine patch was required for this correction.

## Regression Evidence

- Original synthetic fixture: 44 checks for the narrow island and 44 for the
  enclosing island pass. The narrow negative control must exhibit the partial
  `STACK_ESCAPE`; the broad case must finish the channel loop, process the next
  channel, preserve stack bytes, and return balanced. Both normal-handler paths
  are tested. Artifact: `build/sound-stack-final/`.
- Preserved old executable with shooting/movement: regression FAIL, repeated
  `$91F3` and `$DCDF` stack escapes, first pair at frame 838. Artifact:
  `build/first-level-runs/combat-z2vif6tz/summary.json`.
- Rebuilt executable with the same shooting/movement script: PASS through frame
  3575, no BRK and no targeted sound/NMI escape. Artifact:
  `build/first-level-runs/combat-eazm8r3l/summary.json`.
- Early two-Start headless replay: PASS through frame 1841. Artifact:
  `build/first-level-runs/early-jfzqig26/summary.json`.
- Delayed two-Start headless replay: PASS through frame 2561. Artifact:
  `build/first-level-runs/delayed-r8ntpaqu/summary.json`.
- Delayed two-Start windowed replay with the audio device active: PASS through
  frame 2561. Corridor screenshots inspected; no black screen at capture points.
  Artifact: `build/first-level-runs/delayed-sibe7p05/summary.json`.
- Windowed shooting/movement replay with audio active: PASS through frame 3575,
  no BRK, corridor still rendered in the final capture. Artifact:
  `build/first-level-runs/combat-j17jr5jw/summary.json`.
- Existing harness tests: 56 passed; mapper CTest passed; 180-frame selftest
  gates 1/2/3 passed. Artifact: `build/cosim-runs/selftest-9p1tbxo8/summary.json`.

The regression runner enables `NESRECOMP_BRK=fatal`, retains logs/screenshots,
requires script completion, and rejects the sound/NMI escape signatures in a
diagnostic build. Its PASS is not an image-equivalence claim. Production builds
do not include the native stack-trace instrumentation; use the default headless
diagnostic run for that check, and `--windowed` for presentation smoke coverage.

Re-run:

```bash
python3 tools/check_first_level.py
python3 tools/check_first_level.py --delayed
python3 tools/check_first_level.py --combat
python3 tools/check_first_level.py --delayed --windowed
python3 tests/synthetic_sound_stack.py --out build/sound-stack-recheck --recompiler build/recompiler/NESRecomp
```

Use a fresh output directory for the synthetic test. The replay runner creates
its own unique directory. `--exe` can select a preserved prior binary for a
before/after check without rebuilding or reverting sources.

## Remaining Differences

The 900-frame independent comparison still fails at the same earliest palette,
RAM, nametable, OAM, and cycle checkpoints as the bootstrap snapshot:
`build/cosim-runs/compare-wu0l3tkl/summary.json`. Mesen was also run through the
combat script (`build/oracle/first-level-oracle-combat.png`); visual HUD and
presentation differences remain. That capture is not guest-phase aligned with
the native screenshots and is not a bit-exact comparison. Full first-level
completion and exact reproduction of the user's BRK timing remain unproven.
