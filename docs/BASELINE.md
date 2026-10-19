# Bootstrap Baseline

This is the historical bootstrap snapshot. The later sound-stack fix and
first-level replay evidence are recorded in [FIRST_LEVEL.md](FIRST_LEVEL.md).
The initial independent disagreements below still occur after that fix.

## Revisions And Inputs

| Component | Identity |
| --- | --- |
| Mario reference | `9152ffe93004c4b3c060751b18bc0b333b32869f` |
| NESRecomp | `1c7f3a266edc0584119adb0105475994c33676aa` plus `patches/nesrecomp.patch` |
| recomp-ui | `8b08d327e032ede53a55cbf81aa606f3a36a4839` |
| Mesen libretro | `0102910c39ad1a62bc3f784466f3f67ca9eae335` |
| Core binary SHA-256 | `83d83463a7b43a30de556ac74577517b45d252c4b44f465762abd2906627427a` |
| ROM SHA-256 | `5dd3fc3140066c3574b69557d3ebfe08fb0d110eaf8e309634ab1398eb0857b7` |
| Host | Intel x86_64 macOS, Apple Clang 21, SDL2 |

Binary hashes and exact commands are recorded in each local `summary.json`.
Core source is pinned, but system compilers/SDL are not hermetic dependencies.

## Passing Checks

- Standard launcher and developer builds compile and link successfully.
- Mapper register/state test passes under CTest.
- Synthetic UxROM suite: 477 checks, zero failures, plus passing RMW bus sequence.
- Strict harness unit suite: 56 tests pass.
- Live 180-frame selftest: native repeat, oracle repeat, and fault detection pass.
  RAM and OAM XOR faults first appear at guest frame 60 in `ram` and `ppu_mem`.
- Native boot renders the title screen and completes 900 smoke callbacks with
  zero dispatch misses. It emits guest frames 1 through 918; these are different
  clocks, not 918 NMI callbacks or 900 interchangeable frame checkpoints.
- Production 120-frame smoke completes with zero misses even when co-sim freeze,
  hash, and injection environment variables are set; no hash file is produced.
- Engine patch applies cleanly to a separate checkout of the exact base revision.

Final selftest evidence: `build/cosim-runs/selftest-r031_3o0/summary.json`.
Mapper evidence: `build/uxrom-final-verification/results.log` and `mapper.csv`.
Production evidence: `build/game/production-smoke.json`.
Title captures: `build/cosim/sound-islands120.png` and `sound-islands900.png`.
The standard build also completed a neutral-input windowed run using
`tests/title.script`, opened its audio device, and produced the visually checked
`build/game/title-windowed.png`. This is presentation smoke coverage, not a
gameplay or audio-fidelity test.

The RAM fault changes subsequent guest-time trajectory. Fault tests require equal
emission prefixes through injection and successful requested-run completion on
both sides; post-fault cascades may differ. Normal repeat/independent checks do
not loosen coverage. A 900-frame injected run did not complete within the outer
test timeout and was terminated; it is not a passing long fault baseline. Use
the proven 180-frame selftest and retain explicit timeouts for longer experiments.

## Independent Comparison: FAIL

Command:

```bash
python3 tools/cosim.py compare --frames 900 --timeout 30
```

Evidence: `build/cosim-runs/compare-ix2ycn7_/summary.json`.
Native completes 900 callbacks, oracle completes 900 guest frames. Comparison
uses guest frames **1 through 899**, stride **1**, offset **0**, **no RAM/stack
mask**, raw palette/physical nametables, and a **2-cycle interval tolerance**.

| Channel | First mismatch | Native | Oracle |
| --- | --- | --- | --- |
| Palette | guest frame 1, palette byte 0 (`$3F00`) | `00` | `09` |
| RAM | guest frame 4, CPU `$0004` | `02` | `00` |
| Cycle interval | guest frames 4 to 5 | 29,783 cycles | 29,779 cycles |
| Nametable | guest frame 15, first physical NT byte | `20` | `00` |
| OAM | guest frame 19, sprite 0 Y | `F8` | `00` |

These are first disagreements at the stated sampling contract, not proof that
all five subsystems have independent defects. Power-on state, sampling phase,
and true execution divergence need to be distinguished. They are not masked,
automatically realigned, or averaged into a success percentage.

The oracle's old fixed-offset savestate probe was incorrect for this cartridge.
The ported adapter parses and validates Mesen 0.9.9 MST v13 variable-length blocks
instead. It rejects unknown layouts. Its independent traces currently cover
2 KiB CPU RAM, OAM, palette, physical nametables, and CPU cycle counts only.

## Next Debugging Session

Start with the first RAM window (`--start 1 --end 8 --stride 1`) and inspect
zero-page `$04` alongside conflict-safe bank writes and the `$D76F` trampoline.
Keep the initial palette difference visible while determining a justified common
power-on state. Investigate the sound-stack escape separately; title-screen
appearance is not evidence of correct sound or game progression.

No RNG seeding or input was forced. This baseline is boot/title coverage, not a
verified gameplay/attract-demo traversal. All screenshots, traces, binary copies,
and ROM snapshots remain ignored local artifacts. No successful independent
equivalence baseline or release claim has been made.
