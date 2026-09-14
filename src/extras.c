#include "game_extras.h"
#include "nes_runtime.h"
#include "debug_server.h"
#include "mapper.h"
#include "ppu_dot.h"
#include <stdlib.h>

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
