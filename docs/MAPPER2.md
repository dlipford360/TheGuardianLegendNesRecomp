# Mapper 2 Investigation

## Cartridge Identity

Verified local iNES header: `4e45531a080021000000000000000000`.
PRG CRC32 (excluding the header): `FA43146B`. Eight 16 KiB banks; fixed last bank
7 at `$C000-$FFFF`, selected bank at `$8000-$BFFF`, 8 KiB CHR RAM, vertical
nametable mirroring. Vectors: RESET `$FEDE`, NMI `$DC75`, IRQ `$DCF3`.

The iNES header does not distinguish bus-conflict variants. Pinned Mesen's
`Libretro/MesenDB.txt` identifies this PRG CRC as UNROM/submapper 2.
`Core/UNROM.h` enables conflicts for that board; `BaseMapper::WriteRAM` ANDs a
write with the currently mapped ROM byte before switching. `game_on_init()`
therefore explicitly enables `mapper_set_bus_conflicts(1)`. The framework default
remains off for other consumers; the immutable cartridge policy survives state
loads and is reset on cartridge initialization.

The game's `$D705` routine writes through the ROM table at `$FFF2,Y` with a value
read from that same address, so ordinary bank writes are conflict-safe. This
does not justify omitting conflict behavior in a mapper implementation.

## Engine Corrections

Upstream `1c7f3a2` already had basic UxROM registers, despite its stale README.
The patch fixes defects reproduced with original synthetic code:

- Re-check live bank identity before/after low-window instruction boundaries;
  hand changed continuations to the interpreter without another JSR push.
- Reject singleton native dispatch entries belonging to another bank.
- Interpret last-bank aliases in the low window rather than reusing high-window
  native bodies with wrong JSR return-PC constants.
- Apply explicit bus-conflict policy against the pre-write mapping.
- Issue original-value and modified-value RMW writes in mapper-2 PRG space, for
  both native and interpreted execution. Each write sees the current mapping.

The synthetic suite has 477 passing checks plus a write-sequence check. It
covers IRQ-boundary remapping, saved stack data, secondary entries, fallback
disabled, both conflict policies, and native/interpreted RMW variants.
Unrelated peripheral functions are stubbed: this is not a PPU/APU accuracy test.

## Game Integration

`$D76F`, also reached through JMP thunk `$C006`, consumes inline bytes
`bank, target-minus-one-low, target-minus-one-high` after a JSR. It saves/restores
the bank in zero-page `$04` and dispatches via RTS. Configuring this trampoline
prevents inline data from being emitted as instructions. Four explicit bank
entry-table JMP stubs supply targets missed by discovery.

Sound routines bank 0 `$917C`, `$92F7`, and `$97E4` remain deliberate interpreter
islands. The enclosing `$917C` island is required because an end-channel command
abandons its decoder caller and returns to the channel loop, not to the JSR
continuation. Decoder-only interpretation exits halfway through that stack
discard. See [the regression investigation](FIRST_LEVEL.md). Reset and the main
game remain native. Do not report zero dispatch misses as an all-native result.

## Remaining Work

- First independent RAM mismatch: guest frame 4, `$0004`, native 2 vs Mesen 0.
  Investigate boot sampling phase and trampoline instruction/cycle semantics;
  no cause has been proven merely from the mismatch.
- The previously observed `$91F3 -> $92F7` / `$DCDF` sound-stack escape is fixed
  by the enclosing sound-update island and covered by narrow/broad synthetic
  tests plus first-level replays. Other stack-bail sites and independent
  divergences remain to be investigated.
- Discovery still over-produces 4,222 entries, including 684 automatic fallback
  wrappers. Add verified data regions/table metadata rather than editing output.
- Native remapping handoff requires `push_all_jsr=true`. An interrupt remapping
  to an opcode with a different cycle cost can still retain the old charged
  budget; instruction-level timing is not certified.
- Co-simulation lacks independent CPU registers, mapper state, CHR RAM, PPU
  registers, APU, SRAM, and open-bus extraction. Extend the pinned oracle state
  adapter before claiming full-machine identity.
- Scripted first-level startup, shooting, and movement have been exercised.
  Full gameplay, audio fidelity, and presentation equivalence remain unverified.
