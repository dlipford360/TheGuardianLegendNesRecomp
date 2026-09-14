#!/usr/bin/env python3
"""Compile and execute original UxROM probes, without entering RESET.

Run: python3 tests/synthetic_uxrom.py --out build/synthetic-uxrom
Requires cmake and a Unix C compiler. Builds an isolated recompiler from current
sources, then links generated C with the REAL runtime, mapper, and interpreter.
Only unrelated audio/video/debug/override services are stubbed. No game ROM is
read. Artifacts and logs are retained; exit 1 means a semantic check failed.

The fixture is NES 2.0 mapper 2/submapper 2, 128 KiB PRG, 8 KiB CHR RAM.
Conflict expectations follow pinned Mesen UNROM.h/BaseMapper::WriteRAM.
The harness explicitly enables the board policy, just as game_on_init must do.
Both conflict and no-conflict policies are exercised.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


HARNESS = r'''
#include "nes_runtime.h"
#include "mapper.h"
#include "interp.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern uint16_t g_rts_target;

/* Peripheral stubs only: CPU boundaries, memory accesses, dispatch, mapper,
 * stack semantics, and fallback execution are production implementations. */
const char *g_last_recomp_func;
const char *g_recomp_stack[1];
int g_recomp_stack_top;
char g_exe_dir[260] = ".";
int g_dot_ppu_on, g_ws_oam_sidecar;
int g_ws_obj_delta_min, g_ws_obj_delta_max;
int g_render_width = 256;
int16_t g_oam_x16[64], g_ws_shadow_x16[64], g_ws_obj_true_rel;
uint8_t g_ws_obj_rel8, g_ws_obj_ctx_valid;
static uint16_t irq_pc;
static int irq_skip, irq_fires;
void apu_init(void) {}
void apu_clock_cycles(int c) { (void)c; }
int apu_take_dmc_stall(void) { return 0; }
int apu_irq_asserted(void) {
    uint16_t pc = 0;
    int charged = 0;
    runtime_get_savestate_resume(&pc, &charged);
    if (!irq_pc || pc != irq_pc) return 0;
    if (irq_skip) { irq_skip--; return 0; }
    irq_pc = 0; irq_fires++;
    return 1;
}
int apu_get_state_blob(uint8_t *p, int n) { (void)p; (void)n; return 0; }
uint8_t apu_read_status(void) { return 0; }
void apu_write(uint16_t a, uint8_t v) { (void)a; (void)v; }
void save_ram_mark_dirty(void) {}
void nes_foreign_trace_init_dump(void) {}
void nes_vblank_callback(void) { fputs("unexpected frame callback\n", stderr); exit(2); }
int game_dispatch_override(uint16_t a) { (void)a; return 0; }
uint8_t game_ram_read_hook(uint16_t pc, uint16_t a, uint8_t v) {
    (void)pc; (void)a; return v;
}
void debug_server_request_pause(const char *s) { (void)s; }
void ppu_dot_advance(uint32_t n) { (void)n; }
int chr_override_active(void) { return 0; }
void chr_override_on_ppuaddr(uint16_t a) { (void)a; }
void chr_override_on_chr_write(uint16_t a, uint8_t v) { (void)a; (void)v; }
int ppu_predict_spr0_hit_scanline(void) { return 240; }

static uint8_t prg[8 * 0x4000];
static int checks, failures;
static void check(const char *name, unsigned got, unsigned expected) {
    checks++;
    printf("%s %-38s got=%02X expected=%02X\n",
           got == expected ? "PASS" : "FAIL", name, got, expected);
    failures += got != expected;
}
static void fresh(int bank) {
    memset(g_ram, 0, sizeof(g_ram));
    memset(&g_cpu, 0, sizeof(g_cpu));
    g_cpu.S = 0xFD;
    g_cpu.I = 1;
    /* A host-invoked subroutine has one synthetic guest return operand. */
    g_ram[0x1FE] = 0x00;
    g_ram[0x1FF] = 0xC0;
    g_rts_target = g_rti_target = 0;
    g_bail_active = 0;
    irq_pc = 0; irq_skip = irq_fires = 0;
    nes_interp_reset_context();
    mapper_set_bank(bank);
}
static void invoke(uint16_t pc) {
    if (!call_by_address(pc)) {
        fprintf(stderr, "unhandled probe entry %04X\n", pc);
        failures++;
    }
    check("guest stack balanced", g_cpu.S, 0xFF);
}
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    FILE *f = fopen(argv[1], "rb");
    if (!f || fseek(f, 16, SEEK_SET) || fread(prg, 1, sizeof(prg), f) != sizeof(prg))
        return 2;
    fclose(f);
    mapper_init(prg, 8, 2, 1);
    mapper_init_chr(NULL, 0);
    /* Bank-remap handoffs are intentional execution, not optional misses. */
    nes_interp_set_enabled(0);
    nes_write(0x9000, 7);
    check("init defaults to no conflicts", g_current_bank, 7);
    mapper_set_bus_conflicts(1);
    MapperState saved;
    mapper_get_state(&saved);
    mapper_set_state(&saved);
    fresh(0); nes_write(0x9000, 7);
    check("state restore preserves board policy", g_current_bank, 1);

    fresh(0); invoke(0xC100);
    check("fixed -> selected low bank JSR", g_ram[0x10], 0x11);
    fresh(0); invoke(0xC180);
    check("fixed -> selected low bank JMP", g_ram[0x10], 0x11);

    uint64_t cycles;
    fresh(0); g_cpu.A = 1; cycles = g_nes_cycles; invoke(0x8100);
    check("inline STA switches next fetch", g_ram[0x11], 0x21);
    check("inline STA actually changed bank", g_current_bank, 1);
    check("pre-boundary handoff cycles", (unsigned)(g_nes_cycles - cycles), 15);
    fresh(0); invoke(0x8103);
    check("secondary native entry remains usable", g_ram[0x11], 0x20);
    fresh(0); cycles = g_nes_cycles; invoke(0x8200);
    check("fixed callee switches return bank", g_ram[0x12], 0x21);
    check("callee actually changed bank", g_current_bank, 1);
    check("post-call handoff cycles", (unsigned)(g_nes_cycles - cycles), 29);
    fresh(0); invoke(0x8400);
    check("handoff preserves native PHA frame", g_ram[0x16], 0x21);

    fresh(1); invoke(0x8300);
    check("singleton dispatch uses live bank", g_ram[0x13], 0x31);
    fresh(1); check("forced interpreter handled entry", nes_interp_force(0x8300), 1);
    check("interpreter reads correct bank", g_ram[0x13], 0x31);
    check("interpreter stack balanced", g_cpu.S, 0xFF);

    fresh(0); g_cpu.I = 0; irq_pc = 0x8300; cycles = g_nes_cycles;
    invoke(0x8300);
    check("native boundary IRQ changes bank", g_ram[0x13], 0x31);
    check("native boundary IRQ delivered once", irq_fires, 1);
    check("native handoff no double charge", (unsigned)(g_nes_cycles - cycles), 23);
    fresh(0); g_cpu.I = 0; irq_pc = 0x8300; cycles = g_nes_cycles;
    check("interpreter IRQ entry handled", nes_interp_force(0x8300), 1);
    check("interpreter refetch after IRQ", g_ram[0x13], 0x31);
    check("interpreter IRQ stack balanced", g_cpu.S, 0xFF);
    check("interpreter IRQ no double charge", (unsigned)(g_nes_cycles - cycles), 23);
    fresh(0); g_cpu.I = 0; irq_pc = 0x8700; irq_skip = 1;
    invoke(0x8700);
    check("idle loop remap exits old code", g_ram[0x17], 0x42);

    fresh(7); invoke(0x8500);
    check("last-bank low alias pushes CPU PC", g_ram[0x15], 0x85);

    /* FFF0 contains FF: safe on both conflict and non-conflict boards. */
    fresh(0); nes_write(0xFFF0, 0xFE);
    check("bank register wraps to 3 bits", g_current_bank, 6);
    check("fixed bank stays fixed", nes_read(0xFFF0), 0xFF);
    check("CHR RAM selected", mapper_is_chr_ram(), 1);

    /* Mesen: latch = CPU value & pre-write ROM byte. Both windows matter. */
    fresh(0); nes_write(0x9000, 7); /* bank 0 byte = 01 */
    check("low-window bus conflict", g_current_bank, 1);
    fresh(1); nes_write(0x9000, 7); /* bank 1 byte = 03 */
    check("conflict uses OLD selected bank", g_current_bank, 3);
    fresh(0); nes_write(0xFFF1, 7); /* fixed byte = 02 */
    check("fixed-window bus conflict", g_current_bank, 2);

    /* All standard and stable unofficial absolute RMW instructions. Mutate
     * only this test-owned fixture so the OLD bank's ROM byte conflicts but
     * the dummy-write-selected bank presents FF for the final write. */
    const uint8_t old[] = {1,3,0x81,3,0x81,3,0x81,0x81,3,3,3,1};
    const uint8_t modified[] = {2,2,2,1,3,0x81,2,3,1,0x81,2,2};
    const uint8_t accum[] = {0x50,0x50,0x50,0x50,0x50,0x50,0x52,0,0x51,0xD2,0x50,0x4E};
    for (int conflicts = 0; conflicts <= 1; conflicts++) {
        mapper_set_bus_conflicts(conflicts);
        for (int interpreted = 0; interpreted <= 1; interpreted++) {
            for (int i = 0; i < 12; i++) {
                for (int b = 0; b < 8; b++) prg[b * 0x4000 + 0x1000] = 0xFF;
                prg[0x1000] = old[i];
                fresh(0); g_cpu.A = 0x50; g_cpu.C = 1;
                uint16_t pc = (uint16_t)(0xC600 + i * 0x10);
                cycles = g_nes_cycles;
                printf("RMW i=%d interpreted=%d conflicts=%d\n", i, interpreted, conflicts);
                if (interpreted) check("RMW interpreter handled", nes_interp_force(pc), 1);
                else invoke(pc);
                check("RMW bank", g_current_bank, modified[i] & 7);
                check("RMW accumulator", g_cpu.A, accum[i]);
                check("RMW carry", g_cpu.C, i == 9 ? 0 : 1);
                check("RMW zero", g_cpu.Z, i == 7 ? 1 : 0);
                check("RMW negative", g_cpu.N, (i == 5 || i == 9) ? 1 : 0);
                check("RMW overflow", g_cpu.V, 0);
                check("RMW stack", g_cpu.S, 0xFF);
                check("RMW cycles", (unsigned)(g_nes_cycles - cycles), 12);
            }
        }
    }
    mapper_set_bus_conflicts(0);
    fresh(0); nes_write(0x9000, 7);
    check("setter disables conflicts", g_current_bank, 7);
    /* Reinitialization resets policy without extending the saved-state ABI. */
    mapper_set_bus_conflicts(1);
    mapper_init(prg, 8, 2, 1);
    nes_write(0x9000, 7);
    check("reinit resets conflicts", g_current_bank, 7);
    mapper_set_bus_conflicts(1);
    prg[0x1000] = 1; prg[0x5000] = 3;
    fresh(0); invoke(0xC600);      /* INC $9000 */
    /* NMOS RMW writes old=1 (select bank1), then new=2 & bank1[9000]=3. */
    check("RMW conflict final bank", g_current_bank, 2);
    printf("RESULT checks=%d failures=%d\n", checks, failures);
    return failures ? 1 : 0;
}
'''


def fixture(out):
    prg = bytearray(8 * 0x4000)

    def put(bank, addr, code):
        start = bank * 0x4000 + (addr & 0x3FFF)
        data = bytes.fromhex(code)
        prg[start:start + len(data)] = data

    # Only IRQ is invoked. RESET is never entered.
    put(7, 0xC000, "4c 00 c0")
    put(7, 0xFFFA, "00 c0 00 c0 00 c7")
    put(7, 0xFFF0, "ff 02")
    put(7, 0xC100, "a9 01 8d f0 ff 20 00 80 60")
    put(7, 0xC180, "a9 01 8d f0 ff 4c 00 80")
    put(7, 0xC200, "a9 01 8d f0 ff 60")
    for bank in (0, 1):
        put(bank, 0x8000, f"a9 {0x10 + bank:02x} 85 10 60")
        put(bank, 0x8100, f"8d f0 ff a9 {0x20 + bank:02x} 85 11 60")
        put(bank, 0x8200, f"20 00 c2 a9 {0x20 + bank:02x} 85 12 60")
        put(bank, 0x8300, f"a9 {0x30 + bank:02x} 85 13 60")
        put(bank, 0x8400, f"48 a9 01 8d f0 ff 68 a9 {0x20 + bank:02x} 85 16 60")
        put(bank, 0x8500, "60")
    put(0, 0x9000, "01")
    put(1, 0x9000, "03")
    put(7, 0xC400, "ba bd 02 01 85 15 60")
    put(7, 0xC500, "20 00 c4 60")
    for i, opcode in enumerate((0xEE, 0xCE, 0x0E, 0x4E, 0x2E, 0x6E,
                                0x0F, 0x2F, 0x4F, 0x6F, 0xCF, 0xEF)):
        put(7, 0xC600 + i * 0x10, f"{opcode:02x} 00 90 60")
    put(7, 0xC700, "a9 01 8d f0 ff 40")
    put(7, 0xC720, "a9 42 85 17 60")
    put(0, 0x8700, "4c 00 87")
    put(1, 0x8700, "4c 20 c7")
    header = bytes.fromhex("4e45531a080021082000000700000000")
    (out / "uxrom.nes").write_bytes(header + prg)
    config = '[game]\noutput_prefix = "uxrom"\npush_all_jsr = true\ndisable_ptr_scan = true\n'
    # 8000 variants are discovered from fixed-bank control flow, not hints.
    # 8300 has only bank0 generated, although bank1 contains valid code too.
    entries = [(7, 0xC100), (7, 0xC180), (0, 0x8100), (0, 0x8200),
               (0, 0x8300), (0, 0x8400), (0, 0x8700),
               (0, 0x8500), (1, 0x8500), (7, 0xC500)]
    entries += [(7, 0xC600 + i * 0x10) for i in range(12)]
    for bank, addr in entries:
        config += f"\n[[extra_func]]\nbank = {bank}\naddr = 0x{addr:04X}\n"
    config += "\n[[extra_label]]\nbank = 0\naddr = 0x8103\n"
    (out / "game.toml").write_text(config)
    (out / "harness.c").write_text(HARNESS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new artifact directory")
    parser.add_argument("--recompiler", type=Path, help="use an existing executable instead of building")
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    args = parser.parse_args()
    out = args.out.resolve()
    # Do not overwrite another run's logs or the parent's generated artifacts.
    out.mkdir(parents=True, exist_ok=False)
    fixture(out)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NESRECOMP_", "NESREF_"))}

    def run(command, log):
        with (out / log).open("w") as f:
            result = subprocess.run([str(x) for x in command], cwd=out, env=env,
                                    stdout=f, stderr=subprocess.STDOUT, timeout=120)
        if result.returncode:
            print((out / log).read_text(), end="")
            raise RuntimeError(f"command failed ({result.returncode}), see {out / log}")

    engine = ROOT / "nesrecomp"
    recompiler = args.recompiler.resolve() if args.recompiler else out / "recompiler/NESRecomp"
    if not args.recompiler:
        run(["cmake", "-S", engine / "recompiler", "-B", out / "recompiler",
             "-DCMAKE_BUILD_TYPE=Release"], "configure.log")
        run(["cmake", "--build", out / "recompiler", "--parallel", "4"], "build.log")
    run([recompiler, out / "uxrom.nes", "--game", out / "game.toml"], "generate.log")
    sources = [out / "harness.c", out / "generated/uxrom_full.c",
               out / "generated/uxrom_dispatch.c", engine / "runner/src/runtime.c",
               engine / "runner/src/mapper.c", engine / "runner/src/interp.c",
               engine / "runner/src/crc32.c", engine / "recompiler/src/cpu6502_decoder.c"]
    dead_strip = "-Wl,-dead_strip" if sys.platform == "darwin" else "-Wl,--gc-sections"
    run([args.cc, "-std=c11", "-O1", "-ffunction-sections", "-fdata-sections",
         "-I", engine / "runner/include", "-I", engine / "recompiler/src",
         *sources, dead_strip, "-o", out / "probe"], "compile.log")
    with (out / "results.log").open("w") as f:
        env["NESRECOMP_MAPPER_TRACE"] = str(out / "mapper.csv")
        result = subprocess.run([str(out / "probe"), str(out / "uxrom.nes")],
                                cwd=out, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=20)
    results = (out / "results.log").read_text()
    for line in results.splitlines():
        if not line.startswith(("PASS ", "RMW i=")):
            print(line)
    # The RMW final bank alone is coincidentally right without bus conflicts.
    # Require both original-value and modified-value writes to reach the mapper.
    switches = [line.split(",")[1] for line in (out / "mapper.csv").read_text().splitlines()
                if line.startswith("UXROM_PRG,")]
    rmw_ok = switches[-2:] == ["bank=1", "bank=2"]
    rmw_result = f"{'PASS' if rmw_ok else 'FAIL'} RMW bus sequence: {switches[-2:]} expected bank=1,bank=2"
    print(rmw_result)
    with (out / "results.log").open("a") as f:
        f.write(rmw_result + "\n")
    print(f"Artifacts: {out}")
    return result.returncode or (0 if rmw_ok else 1)


if __name__ == "__main__":
    sys.exit(main())
