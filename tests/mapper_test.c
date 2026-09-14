#include "mapper.h"
#include "nes_runtime.h"
#undef NDEBUG
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

uint64_t g_frame_count;
const char *g_last_recomp_func;
uint8_t g_chr_ram[0x2000];

int main(void) {
    static uint8_t prg[8 * 0x4000];
    for (int b = 0; b < 8; b++) memset(prg + b * 0x4000, b, 0x4000);
    mapper_init(prg, 8, 2, 1);
    mapper_init_chr(NULL, 0);
    assert(mapper_is_chr_ram());
    assert(mapper_get_mirroring() == 2);
    memset(g_chr_ram, 0x5A, sizeof(g_chr_ram));
    for (int b = 0; b < 256; b++) {
        mapper_write(0x8000 + b, (uint8_t)b);
        assert(g_current_bank == (b & 7));
        assert(mapper_peek_prg(0x8000) == (b & 7));
        assert(mapper_peek_prg(0xBFFF) == (b & 7));
        assert(mapper_peek_prg(0xC000) == 7);
        assert(mapper_peek_prg(0xFFFF) == 7);
        assert(mapper_get_mirroring() == 2);
        for (int i = 0; i < 0x2000; i++) assert(g_chr_ram[i] == 0x5A);
    }
    mapper_write(0xFFFF, 3);
    mapper_write(0x7FFF, 4);
    assert(g_current_bank == 3);
    MapperState state = {0};
    mapper_get_state(&state);
    mapper_write(0xC000, 6);
    mapper_set_state(&state);
    assert(mapper_peek_prg(0x8000) == 3);
    assert(mapper_peek_prg(0xC000) == 7);
    mapper_clock_cpu(10000);
    assert(!mapper_irq_asserted());
    assert(!mapper_clock_scanline());
    mapper_init(prg, 8, 2, 0);
    assert(g_current_bank == 0);
    assert(mapper_get_mirroring() == 3);
    puts("UxROM register/mapping/state tests passed (bus conflicts not modeled)");
    return 0;
}
