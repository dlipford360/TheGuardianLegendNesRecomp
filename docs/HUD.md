# Bottom HUD Rendering

## Cause

The first corridor's status bar is not a host UI overlay. The game renders its
own text, health, and icons using a mid-frame PPU split. `$DD21` writes `$27,$00`
to `$2006`, selects nametable 1 through `$2000`, disables sprites via `$2001`,
and writes zero scroll to `$2005`. This path is reached after sprite-0 polling,
including through the DMC IRQ handler at `$DCF3` and the main-loop wait path.

The completed `$2006` pair reloads the live PPU address `v` immediately. Later
`$2005` writes update temporary address `t`, not the live vertical counter. The
old scanline renderer watched only the scroll variables, missing the live `v`
reload. The default once-per-frame compositor also could not represent both
the already-painted playfield and the late status bar.

## Correction

- `runtime_get_ppu_v_at_write()` exposes the address from the last completed
  `$2006` pair, alongside its existing write epoch. Partial writes and later
  `$2007` increments cannot overwrite this recorded address.
- The scanline renderer detects a new write epoch during a visible frame and
  reloads coarse Y, fine Y, and the vertical nametable bit. It advances that
  counter instead of replacing it with subsequent t-only scroll changes.
- Repeated writes to the same address still reload. Off-screen snapshots save
  and restore the new bookkeeping; frame boundaries discard the raster override.
- `game_on_init()` uses the existing `ppu_dot_set_default_enabled(1)` API. This
  enables the corrected scanline path for Guardian Legend without changing
  other games' default renderer. No ROM patch, hardcoded HUD text, or synthesized
  health/score values were added.

This is the framework's scanline-granular renderer, not a claim of complete
dot/subpixel timing accuracy. `NESRECOMP_DOT_PPU=0` restores the legacy path for
diagnosis and also restores its missing-HUD limitation.

## Verification

`tests/ppu_raster_test.c` uses original synthetic nametables and tiles, not game
assets. It checks the exact boundary pixels, fine-Y offset, later t-only writes,
same-value address reloads, snapshot isolation, and next-frame reset. CTest
`ppu_raster_v` passes with the correction. Linking that test to the preserved old
renderer fails its first white-HUD-row assertion at scanline 208.

The two-Start shooting/movement replay now displays the black bar with score,
chips, shot, health, area, and weapon data, through logical frame 3575:

- Headless: `build/first-level-runs/combat-69lujdz_/summary.json` and its
  `first-level-combat-late.png`.
- Windowed with audio: `build/first-level-runs/combat-2ziycdha/summary.json` and
  its `first-level-combat-late.png`.

Both replays complete with no BRK and no targeted sound/NMI stack escape. Their
late screenshots were visually inspected. CTest's mapper and raster tests pass;
56 harness unit tests pass; co-simulation gates 1/2/3 pass over 180 frames
(`build/cosim-runs/selftest-vb60z9vl/summary.json`).

The independent 900-frame state comparison remains red at the previously
documented initial checkpoints (`build/cosim-runs/compare-5kbhl2c4/summary.json`).
Visible HUD restoration is not a full-game or timing-equivalence certification.
The updated engine changes are exported in `patches/nesrecomp.patch`.
