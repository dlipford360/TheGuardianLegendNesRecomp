/* Original synthetic pixel test; no game ROM or assets are used. */
#include "nes_runtime.h"
#include "ppu_dot.h"
#undef NDEBUG
#include <assert.h>
#include <stdio.h>
#include <string.h>

uint8_t g_ppuctrl, g_ppumask, g_ppustatus, g_ppuscroll_x, g_ppuscroll_y;
uint16_t g_ppuaddr;
uint8_t g_ppu_nt[0x1000], g_ppu_oam[0x100], g_ppu_pal[0x20], g_chr_ram[0x2000];
int g_render_width = 256, g_widescreen_left, g_widescreen_right;
int g_ws_eff_left, g_ws_eff_right, g_ws_oam_sidecar, g_disable_render_irq;
int16_t g_oam_x16[64];
const uint32_t g_nes_palette[64] = {0xFF000000, 0xFF0000FF, 0xFFFFFFFF, 0xFFFF0000};
static uint64_t epoch;
static uint16_t written_v;

uint64_t runtime_get_ppu_v_write_epoch(void) { return epoch; }
uint16_t runtime_get_ppu_v_at_write(void) { return written_v; }
int mapper_get_mirroring(void) { return 2; }
int mapper_clock_scanline(void) { return 0; }
void runtime_call_irq_handler(void) { assert(!"unexpected IRQ"); }
void runtime_sync_scroll_from_v(void) { assert(!"unexpected MMC3 sync"); }
int ppu_renderer_sprite_suppressed(int s, int x, int y) { (void)s; (void)x; (void)y; return 0; }
void ppu_renderer_set_background_opaque_frame(const uint8_t *p, int w, int h) {
    (void)p; assert(w == 256 && h == 240);
}

static void through_line(unsigned y) {
    ppu_dot_advance(((21 + y) * 341 + 2) / 3);
}

int main(void) {
    static uint32_t frame[256 * 240], snapshot[256 * 240];
    g_ppumask = 0x0A; /* Background plus left-column visibility; no sprites. */
    g_ppu_pal[1] = 1; g_ppu_pal[2] = 2; g_ppu_pal[3] = 3;
    memset(g_ppu_nt, 1, 0x3C0);                 /* blue playfield in NT0 */
    memset(g_ppu_nt + 0x400 + 24 * 32, 2, 32); /* white HUD row in NT1 */
    memset(g_ppu_nt + 0x400 + 25 * 32, 3, 32); /* red next row */
    memset(g_chr_ram + 16, 0xFF, 8);           /* tile1 = color 1 */
    memset(g_chr_ram + 32 + 8, 0xFF, 8);       /* tile2 = color 2 */
    memset(g_chr_ram + 48, 0xFF, 16);          /* tile3 = color 3 */
    ppu_dot_init(frame);
    g_dot_ppu_on = 1;
    through_line(207);

    /* $2006=$27,$00: NT1, coarse Y=24, fine Y=2. Following $2005
     * writes reset t's Y to zero but must NOT reset this visible v. */
    written_v = 0x2700; epoch++;
    g_ppuctrl = 1; g_ppuscroll_y = 0;
    through_line(208);
    g_ppuscroll_y = 80; /* a later t-only update is also not a live-v reload */
    through_line(214);
    ppu_dot_render_snapshot(snapshot); /* must preserve raster epoch/counters */
    through_line(217);

    /* Same address written again still reloads the live counter. */
    epoch++;
    through_line(218);
    ppu_dot_frame_boundary();
    for (int x = 0; x < 256; x++) {
        assert(frame[207 * 256 + x] == 0xFF0000FF);
        assert(frame[208 * 256 + x] == 0xFFFFFFFF);
        assert(frame[213 * 256 + x] == 0xFFFFFFFF);
        assert(frame[214 * 256 + x] == 0xFFFF0000);
        assert(frame[217 * 256 + x] == 0xFFFF0000);
        assert(frame[218 * 256 + x] == 0xFFFFFFFF);
    }

    /* No stale HUD scroll in the following frame. */
    g_ppuctrl = 0; g_ppuscroll_y = 0;
    ppu_dot_frame_boundary();
    for (int i = 0; i < 256 * 240; i++) assert(frame[i] == 0xFF0000FF);
    puts("PPU raster-v pixels, fine Y, repeat writes, snapshot and frame reset passed");
    return 0;
}
