# The Guardian Legend NES Recomp

Experimental Guardian Legend (USA) port using [NESRecomp](https://github.com/mstan/nesrecomp),
structured after [SuperMarioBrosNESRecomp](https://github.com/mstan/SuperMarioBrosNESRecomp).

**Bootstrap status:** builds on Intel macOS, renders the title screen, and completes
900 no-input smoke frames with zero dispatch misses. This is **not a finished
port**: independent co-simulation currently diverges and gameplay/presentation
differences remain. Native reset and game code run with explicitly configured
sound interpreter islands. Zero dispatch misses does not mean zero interpreted
code. The first-level sound-stack escape is covered by a regression fix; see
[the first-level investigation](docs/FIRST_LEVEL.md).
The missing bottom HUD is corrected by the game's default scanline renderer;
see [HUD rendering](docs/HUD.md).

## Build And Run

Prerequisites: Git, Python 3.10+, CMake 3.20+, C/C++ compiler, and SDL2. On this
Intel Mac the prerequisites were already installed. A typical Homebrew setup is
`brew install cmake sdl2`. Linux/Windows game builds have not been validated here;
the provided oracle build script explicitly targets Intel macOS.

From this repository:

```bash
bash setup.sh
bash build.sh
build/game/GuardianLegendRecomp "Guardian Legend, The (U) [!].nes"
```

`build.sh` verifies the ROM, builds the recompiler, regenerates C, and builds the
game plus the same `recomp-ui` launcher used by the reference project. Run the
executable without arguments to use its ROM-selection launcher. No Mario art,
mods, symbols, generated code, or ROM is used by this port.

To use another path to the same ROM revision:

```bash
bash build.sh --rom "/absolute/path/to/Guardian Legend.nes"
python3 tools/project.py rom-info --rom "/absolute/path/to/Guardian Legend.nes"
```

The original local ROM is left in place and ignored by Git. Generation requires
the verified image with SHA-256:

```text
5dd3fc3140066c3574b69557d3ebfe08fb0d110eaf8e309634ab1398eb0857b7
```

Cartridge: iNES mapper 2, 128 KiB PRG ROM, 8 KiB CHR RAM, vertical mirroring;
headerless PRG CRC32 `fa43146b`. No BIOS is required. ROMs, generated C, extracted
assets, builds, and diagnostic snapshots are not distributed or tracked.

## Co-Simulation

This follows the [Retro Porting Toolkit guide](https://retroportingtoolkit.com/docs/guides/set-up-co-simulation):
use an independent emulator, compare at guest-time checkpoints, prove the red
path, and retain the first mismatch rather than hide it with alignment heuristics.

```bash
bash build.sh --cosim
bash tools/build-oracle.sh
python3 tools/cosim.py selftest --frames 180 --timeout 30
python3 tools/cosim.py compare --frames 900 --timeout 30
```

The selftest should pass. **The independent comparison is currently expected to
fail.** See [the recorded baseline](docs/BASELINE.md) for the exact disagreements.
The oracle is pinned Mesen through the portable `nesref` frontend, not the
framework's own interpreter. It runs as a separate development process.

Each command prints its result and a retained `summary.json` path under
`build/cosim-runs/`. Logs, traces, binary copies, and local-only ROM snapshots are
kept there. Runs isolate executable-relative saves and do not erase player saves.
The harness rejects early exits, timeouts, truncated streams, missing checkpoints,
and incomplete initial snapshots. No game-specific address masks, live input,
RNG freezes, or automatic offsets are inherited.

Exit codes are `0` for PASS, `1` for FAIL, and `2` for UNSUPPORTED. Even if all
currently extracted independent channels agree, unsupported CPU/mapper/CHR and
other channels prevent a full-machine certification. Native hashes cover those
channels only for repeatability, not independent correctness.

To narrow the first RAM divergence without changing alignment:

```bash
python3 tools/cosim.py compare --frames 180 --start 1 --end 8 --stride 1 --timeout 30
```

`--offset N` explicitly compares oracle frame `f+N` to native frame `f`.
`--mask-stack` explicitly excludes CPU RAM `$0100-$01FF`; default comparison is
unmasked. All exclusions and compared-frame counts are recorded. Do not add
exclusions just to make results green. `--cycle-tolerance` is an explicit interval
budget in CPU cycles, default 2, not a frame-alignment control.

Normal `build/game` builds compile out co-sim capture, RAM freezing, and fault
injection (`TGL_COSIM=OFF`), and use the TCP debug-server stub. `build/cosim`
enables those features and diagnostic rings. Optional live TCP debugging is
enabled only with `NESRECOMP_DEBUG=1`, on localhost port 4370. Co-simulation itself
uses deterministic file captures, not TCP or wall-clock synchronization.

Guardian Legend selects the cycle-driven scanline renderer by default because
its bottom status bar changes the PPU address partway through the picture.
`NESRECOMP_DOT_PPU=0` is a diagnostic override to the legacy renderer, not a
recommended setting: that path cannot display this HUD split correctly.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
ctest --test-dir build/cosim --output-on-failure
python3 tests/synthetic_uxrom.py --out build/uxrom-check
python3 tests/synthetic_sound_stack.py --out build/sound-stack-check --recompiler build/recompiler/NESRecomp
python3 tools/check_first_level.py --combat
python3 tools/check_first_level.py --delayed --windowed
```

Use a fresh output directory for each synthetic run. The fixture constructs
original machine-code probes; it never reads the game ROM. It exercises actual
generated C, runtime, mapper, and interpreter code with peripheral stubs.

## Layout

| Path | Purpose |
| --- | --- |
| `game.toml`, `symbols.sym` | Game discovery/configuration and symbol overlay |
| `src/extras.c` | Minimal game hooks and explicit cartridge conflict policy |
| `generated/` | Ignored regenerated code and ROM metadata |
| `nesrecomp/`, `recomp-ui/` | Pinned Git submodules |
| `patches/nesrecomp.patch` | Reproducible engine/oracle changes over the pin |
| `dependencies.json` | Upstream reference and dependency revisions |
| `tools/` | Build, ROM verification, oracle, and strict comparison tools |
| `tests/` | Mapper, synthetic CPU, and harness regressions |
| `docs/` | Baseline, mapper investigation, and remaining issues |

The complete Mario reference clone lives separately at
`../SuperMarioBrosNESRecomp`, with all its recursive submodules including
`smb-disassembly`. Guardian Legend depends only on its own pinned submodules, not
the sibling checkout. There is no new GitHub remote or commit yet.

See [mapper notes](docs/MAPPER2.md) and [patch maintenance](patches/README.md).

## License

Upstream-derived work retains the PolyForm Noncommercial terms and attribution
in `LICENSE`. NESRecomp and recomp-ui retain their own license files and notices.
SDL2, Dear ImGui, and other bundled components retain their upstream terms.
Mesen is a separately built GPL-licensed development oracle; its source and
license remain in the pinned `build/oracle-src/Mesen` checkout, and it is not
linked or packaged into the game. The ROM and original game assets remain their
owners' property and are not included.
