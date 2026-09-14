#!/usr/bin/env python3
"""Original sound-like nonlocal stack regression; no game ROM is read.

Run: python3 tests/synthetic_sound_stack.py --out build/synthetic-sound-stack \
    --recompiler build/recompiler/NESRecomp

Links generated code to the actual runtime, mapper, and interpreter, borrowing
only the peripheral stubs and setup helpers from synthetic_uxrom. The same ROM
is compiled with decoder-only and enclosing-frame force_interp configurations.
The narrow island must report STACK_ESCAPE with next_pc AT the fourth PLA
(after executing the third), not a subroutine RETURN. The enclosing island
must finish the discard/JMP, process the next channel, and return balanced.
Both configurations must also handle the ordinary handler/continuation return.
All machine code below is original test data, not extracted game code.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

from synthetic_uxrom import HARNESS as UXROM_HARNESS, ROOT


# Reuse the real-runtime fixture's peripheral stubs, check(), fresh(), and
# invoke(), but none of its ROM data or test cases. Fail if its split changes.
SUPPORT, SEPARATOR, _ = UXROM_HARNESS.partition("int main(int argc, char **argv) {")
assert SEPARATOR, "synthetic_uxrom harness main signature changed"

HARNESS = SUPPORT + r'''
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    int broad = !strcmp(argv[2], "broad");
    FILE *f = fopen(argv[1], "rb");
    if (!f || fseek(f, 16, SEEK_SET) || fread(prg, 1, sizeof(prg), f) != sizeof(prg))
        return 2;
    fclose(f);
    mapper_init(prg, 8, 2, 1);
    mapper_init_chr(NULL, 0);
    /* Force wrappers must work even when ordinary ROM miss fallback is off. */
    nes_interp_set_enabled(0);
    nes_interp_set_native_handoff_mode(NES_INTERP_HANDOFF_ISLAND);

    for (int end_channel = 0; end_channel <= 1; end_channel++) {
        NesInterpExit exit;
        NesInterpStats before, after;
        fresh(0);
        g_ram[0x20] = (uint8_t)end_channel;
        nes_interp_get_stats(&before);
        printf("CASE %s %s\n", broad ? "broad" : "narrow",
               end_channel ? "end-channel" : "normal-return");
        check("generated frame dispatch handled", call_by_address(0xC100), 1);
        nes_interp_get_last_exit(&exit);
        nes_interp_get_stats(&after);
        check("no native island handoffs",
              (unsigned)(after.native_handoffs - before.native_handoffs), 0);
        check("no interpreter declines",
              (unsigned)(after.declines - before.declines), 0);
        check("no interpreter policy traps",
              (unsigned)(after.policy_traps - before.policy_traps), 0);
        check("no interpreter watchdog trips",
              (unsigned)(after.watchdog_trips - before.watchdog_trips), 0);
        check("host return low preserved", g_ram[0x1FE], 0x00);
        check("host return high preserved", g_ram[0x1FF], 0xC0);
        check("interpreter entry PC", exit.entry_pc, broad ? 0xC100 : 0xC200);
        check("interpreter entry S", exit.entry_s, broad ? 0xFD : 0xFB);

        if (!broad && end_channel) {
            check("narrow exit is STACK_ESCAPE", exit.kind, NES_INTERP_EXIT_STACK_ESCAPE);
            check("next PC is fourth PLA", exit.next_pc, 0xC263);
            check("fourth PLA remains unexecuted", exit.exit_s, 0xFC);
            check("native frame bailed at escape", g_cpu.S, 0xFC);
            check("third PLA consumed JSR low byte", g_cpu.A, 0x04);
            check("JSR high byte still on stack", g_ram[0x100 + g_cpu.S + 1], 0xC1);
            check("RTS sidecar is stale handler target", g_rts_target, 0xC25F);
            check("channel zero completion skipped", g_ram[0x30], 0);
            check("next channel NOT processed", g_ram[0x31], 0);
            check("frame NOT completed", g_ram[0x34], 0);
            check("continuation NOT reached", g_ram[0x35], 0);
            check("channel index has not advanced", g_cpu.X, 0);
            check("one narrow decoder island", (unsigned)(after.runs - before.runs), 1);
        } else {
            check("island exits by RETURN", exit.kind, NES_INTERP_EXIT_RETURN);
            check("architectural return PC", exit.next_pc, broad ? 0xC001 : 0xC105);
            check("island exit stack", exit.exit_s, broad ? 0xFF : 0xFD);
            check("whole frame stack balanced", g_cpu.S, 0xFF);
            check("whole frame return operand", g_rts_target, 0xC000);
            check("no RTI sidecar", g_rti_target, 0);
            check("first channel completion", g_ram[0x30], end_channel ? 0 : 1);
            check("subsequent channel RAM marker", g_ram[0x31], 1);
            check("frame completed exactly once", g_ram[0x34], 1);
            check("decoder continuation count", g_ram[0x35], end_channel ? 1 : 2);
            check("unrelated LDA survives handler RTS", g_ram[0x36], 0xA7);
            check("both channels visited", g_cpu.X, 2);
            check("expected interpreter island count",
                  (unsigned)(after.runs - before.runs), broad ? 1 : 2);
        }
    }
    printf("RESULT %s checks=%d failures=%d\n", argv[2], checks, failures);
    return failures ? 1 : 0;
}
'''


def fixture(out):
    prg = bytearray(8 * 0x4000)

    def put(addr, code):
        data = bytes.fromhex(code)
        start = 7 * 0x4000 + (addr & 0x3FFF)
        prg[start:start + len(data)] = data

    # Never enter RESET/NMI/IRQ. Host invocation uses fresh()'s C000 operand.
    put(0xC000, "4c 00 c0")
    put(0xFFFA, "00 c0 00 c0 00 c0")
    # C100: LDX #0; loop: JSR decoder; INC $30,X;
    # C107: INX; CPX #2; BNE loop; INC $34; RTS.
    put(0xC100, "a2 00 20 00 c2 f6 30 e8 e0 02 d0 f6 e6 34 60")
    # Push continuation operand C21F, then select normal/end handler by $20
    # only for channel zero. Channel one always selects the normal handler.
    # Push handler operand C23F/C25F, then an UNRELATED LDA #A7 before RTS:
    # this is deliberately not an adjacent LDA/PHA/LDA/PHA/RTS idiom.
    put(0xC200, "a9 c2 48 a9 1f 48 a4 20 e0 00 f0 02 a0 00 "
                "a9 c2 48 b9 80 c2 48 a9 a7 60")
    put(0xC220, "e6 35 60")       # continuation: INC $35; RTS to decoder caller
    put(0xC240, "85 36 60")       # normal handler: STA $36; RTS to continuation
    # Discard synthetic continuation AND actual decoder-caller JSR operand.
    # Narrow S_floor=FB: third PLA lifts S to FC, next_pc=C263 (fourth PLA).
    # Broad S_floor=FD: all four PLAs finish at FD, JMP stays inside the island.
    put(0xC260, "68 68 68 68 4c 07 c1")
    put(0xC280, "3f 5f")
    header = bytes.fromhex("4e45531a080021082000000700000000")
    (out / "sound-stack.nes").write_bytes(header + prg)
    (out / "harness.c").write_text(HARNESS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new artifact directory")
    parser.add_argument("--recompiler", type=Path, required=True, help="existing NESRecomp executable")
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    args = parser.parse_args()
    out = args.out.resolve()
    recompiler = args.recompiler.resolve()
    if not recompiler.is_file():
        parser.error(f"recompiler not found: {recompiler}")
    out.mkdir(parents=True, exist_ok=False)
    fixture(out)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NESRECOMP_", "NESREF_"))}
    engine = ROOT / "nesrecomp"
    dead_strip = "-Wl,-dead_strip" if sys.platform == "darwin" else "-Wl,--gc-sections"
    for variant, entry in (("narrow", 0xC200), ("broad", 0xC100)):
        work = out / variant
        work.mkdir()
        config = ('[game]\noutput_prefix = "sound_stack"\npush_all_jsr = true\n'
                  'disable_ptr_scan = true\n'
                  f'\n[force_interp]\nfixed = [0x{entry:04X}]\n')
        for addr in (0xC100, 0xC200, 0xC220, 0xC240, 0xC260):
            config += f"\n[[extra_func]]\nbank = 7\naddr = 0x{addr:04X}\n"
        (work / "game.toml").write_text(config)

        def run(command, log, timeout=120):
            with (work / log).open("w") as f:
                result = subprocess.run([str(x) for x in command], cwd=work, env=env,
                                        stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
            if result.returncode:
                print((work / log).read_text(), end="")
                raise RuntimeError(f"command failed ({result.returncode}), see {work / log}")

        run([recompiler, out / "sound-stack.nes", "--game", work / "game.toml"], "generate.log")
        sources = [out / "harness.c", work / "generated/sound_stack_full.c",
                   work / "generated/sound_stack_dispatch.c", engine / "runner/src/runtime.c",
                   engine / "runner/src/mapper.c", engine / "runner/src/interp.c",
                   engine / "runner/src/crc32.c", engine / "recompiler/src/cpu6502_decoder.c"]
        run([args.cc, "-std=c11", "-O1", "-ffunction-sections", "-fdata-sections",
             "-I", engine / "runner/include", "-I", engine / "recompiler/src",
             *sources, dead_strip, "-o", work / "probe"], "compile.log")
        run([work / "probe", out / "sound-stack.nes", variant], "results.log", timeout=20)
        for line in (work / "results.log").read_text().splitlines():
            if not line.startswith("PASS "):
                print(line)
    print(f"Artifacts: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
