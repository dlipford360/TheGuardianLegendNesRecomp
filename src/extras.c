#include "game_extras.h"
#include "nes_runtime.h"
#include "debug_server.h"
#include "mapper.h"
#include "ppu_dot.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

const char *g_rom_path_for_extras = NULL;
/* Legacy debug-server ABI; this game uses the co-sim process timeout. */
int g_watchdog_triggered;
uint32_t g_watchdog_frame;
const char *g_watchdog_stack_dump = NULL;
static int debug_enabled;

const char *game_get_name(void) { return "The Guardian Legend"; }
uint32_t game_get_expected_crc32(void) { return TGL_ROM_CRC32; }

void game_on_init(void) {
    /* Mesen's pinned database identifies PRG CRC FA43146B as UNROM with
     * bus conflicts. The old iNES header alone cannot encode that policy. */
    mapper_set_bus_conflicts(1);
    /* The bottom HUD changes live v at sprite 0, after the playfield has
     * rendered. A single post-NMI snapshot cannot represent both regions. */
    ppu_dot_set_default_enabled(1);
#if NESRECOMP_TRACE
    const char *debug = getenv("NESRECOMP_DEBUG");
    debug_enabled = debug && debug[0] == '1';
    if (debug_enabled) debug_server_init(4370);
#endif
}

void game_on_frame(uint64_t frame_count) {
    (void)frame_count;
    /* Opt-in PPU-state trace (TGL_PPU_TRACE=1): one stderr line when mask,
     * control, or palette bytes change, plus a 10s heartbeat. Used to
     * diagnose black-screen reports where the game keeps running. */
    static int trace_on = -1;
    static uint8_t prev_mask, prev_ctrl, prev_pal[8];
    static uint64_t prev_logged = 0;
    static int first = 1;
    if (trace_on < 0) {
        const char *e = getenv("TGL_PPU_TRACE");
        trace_on = (e && e[0] == '1') ? 1 : 0;
    }
    if (trace_on) {
        uint8_t pal[8];
        memcpy(pal, g_ppu_pal, 8);
        if (first || g_ppumask != prev_mask || g_ppuctrl != prev_ctrl ||
            memcmp(pal, prev_pal, 8) != 0 ||
            frame_count - prev_logged >= 600) {
            fprintf(stderr,
                    "[TGLPPU] f=%llu bank=%d ctrl=%02X(nmi=%d) mask=%02X(bg=%d sp=%d) "
                    "pal=%02X%02X%02X%02X%02X%02X%02X%02X\n",
                    (unsigned long long)frame_count, g_current_bank,
                    g_ppuctrl, (g_ppuctrl >> 7) & 1,
                    g_ppumask, (g_ppumask & 0x08) ? 1 : 0,
                    (g_ppumask & 0x10) ? 1 : 0,
                    pal[0], pal[1], pal[2], pal[3],
                    pal[4], pal[5], pal[6], pal[7]);
            prev_mask = g_ppumask;
            prev_ctrl = g_ppuctrl;
            memcpy(prev_pal, pal, 8);
            prev_logged = frame_count;
            first = 0;
        }
    }
    if (debug_enabled) {
        debug_server_poll();
        debug_server_wait_if_paused();
        int input = debug_server_get_input_override();
        if (input >= 0) g_controller1_buttons = (uint8_t)input;
    }
}
void game_post_nmi(uint64_t frame_count) {
    (void)frame_count;
    if (debug_enabled) debug_server_record_frame();
}
void game_run_nmi(void) { func_NMI(); }
void game_run_main(void) { func_RESET(); }
int game_handle_arg(const char *key, const char *val) { (void)key; (void)val; return 0; }
const char *game_arg_usage(void) { return NULL; }
int game_dispatch_override(uint16_t addr) { (void)addr; return 0; }
uint8_t game_ram_read_hook(uint16_t pc, uint16_t addr, uint8_t val) {
    (void)pc; (void)addr; return val;
}
void game_post_render(uint32_t *framebuf) { (void)framebuf; }
void game_fill_frame_record(void *record) { (void)record; }
int game_handle_debug_cmd(const char *cmd, int id, const char *json) {
    (void)cmd; (void)id; (void)json; return 0;
}
