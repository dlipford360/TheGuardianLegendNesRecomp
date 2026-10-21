#!/usr/bin/env python3
"""Regression for an equal-S JSR returning with a deferred native tail lap.

Links the real runtime; no game ROM is needed. Models a dispatcher that pops
its JSR operand and tail-jumps to an already active ancestor, restoring S but
not returning to the instruction after the JSR.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from synthetic_sound_stack import SUPPORT, ROOT

HARNESS = SUPPORT + r'''
uint16_t g_rts_target, g_rti_target, g_rti_source;
int g_rti_bank = -1;
static int laps, fallthrough;
int call_by_address_cb(uint16_t addr, int bank) {
    (void)bank;
    if (addr != 0xC100) return 0;
    if (++laps == 1) {
        uint8_t before_s = g_cpu.S;
        uint64_t epoch = runtime_get_interrupt_epoch();
        /* Native JSR followed by a dispatcher consuming its operand. */
        g_ram[0x100 + g_cpu.S--] = 0xC1;
        g_ram[0x100 + g_cpu.S--] = 0x02;
        g_cpu.S += 2;
        call_by_address_tail(0xC100, -1);
        if (!nes_jsr_stack_ok_after_call(before_s, epoch)) return 1;
        fallthrough++;  /* Inline data must never execute. */
    } else {
        g_cpu.S++; uint8_t lo = g_ram[0x100 + g_cpu.S];
        g_cpu.S++; uint8_t hi = g_ram[0x100 + g_cpu.S];
        g_rts_target = ((uint16_t)hi << 8) | lo;
    }
    return 1;
}
int main(void) {
    mapper_init(prg, 8, 2, 1);
    mapper_init_chr(NULL, 0);
    fresh(0);
    check("ordinary equal-S return accepted",
          nes_jsr_stack_ok_after_call(g_cpu.S, runtime_get_interrupt_epoch()), 1);
    call_by_address_tail(0xC100, -1);
    check("ancestor consumes deferred lap", laps, 2);
    check("inline data never executed", fallthrough, 0);
    check("guest stack balanced", g_cpu.S, 0xFF);
    check("host return preserved", g_rts_target, 0xC000);
    check("guard restored after lap consumed",
          nes_jsr_stack_ok_after_call(g_cpu.S, runtime_get_interrupt_epoch()), 1);
    return failures ? 1 : 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path,
                        default=ROOT / "nesrecomp/runner/src/runtime.c")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / "harness.c").write_text(HARNESS)
    engine = ROOT / "nesrecomp"
    sources = [out / "harness.c", args.runtime_source.resolve(),
               engine / "runner/src/mapper.c", engine / "runner/src/interp.c",
               engine / "runner/src/crc32.c", engine / "recompiler/src/cpu6502_decoder.c"]
    strip = "-Wl,-dead_strip" if sys.platform == "darwin" else "-Wl,--gc-sections"
    command = [os.environ.get("CC", "cc"), "-std=c11", "-O1",
               "-ffunction-sections", "-fdata-sections", "-I", engine / "runner/include",
               "-I", engine / "runner/src", "-I", engine / "recompiler/src",
               *sources, strip, "-o", out / "probe"]
    with (out / "compile.log").open("w") as log:
        subprocess.run(list(map(str, command)), stdout=log, stderr=subprocess.STDOUT, check=True)
    result = subprocess.run([str(out / "probe")], capture_output=True, text=True, timeout=20)
    (out / "results.log").write_text(result.stdout + result.stderr)
    print(result.stdout + result.stderr, end="")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
