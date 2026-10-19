cmake_minimum_required(VERSION 3.20)
project(GuardianLegendNESRecomp C CXX)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(NESRECOMP_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/nesrecomp")
option(TGL_LAUNCHER "Build the recomp-ui launcher" ON)
option(TGL_COSIM "Compile developer co-simulation capture and fault injection" OFF)
include("${NESRECOMP_ROOT}/runner/runner.cmake")
set_property(SOURCE "${NESRECOMP_ROOT}/runner/src/runtime.c" APPEND PROPERTY
    COMPILE_DEFINITIONS NESRECOMP_COSIM=$<BOOL:${TGL_COSIM}>)
if(WIN32)
    list(APPEND CMAKE_PREFIX_PATH "${NESRECOMP_ROOT}/runner/external/SDL2/cmake")
endif()
find_package(SDL2 REQUIRED)
if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/generated/rom.cmake")
    message(FATAL_ERROR "Run python3 tools/project.py generate first")
endif()
include("${CMAKE_CURRENT_SOURCE_DIR}/generated/rom.cmake")
# Use only the parts named by the current umbrella, not stale files left by
# earlier generations. Compiling these separately bounds Clang memory usage.
file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/generated/guardian-legend_full.c" TGL_PART_LINES
    REGEX "^#include \"guardian-legend_full_bank.*\\.c\"")
set(TGL_PARTS)
foreach(line IN LISTS TGL_PART_LINES)
    string(REGEX REPLACE "^#include \"([^\"]+)\".*" "\\1" part "${line}")
    list(APPEND TGL_PARTS "generated/${part}")
endforeach()
add_executable(GuardianLegendRecomp
    ${NESRECOMP_RUNNER_SOURCES}
    src/extras.c
    ${TGL_PARTS}
    generated/guardian-legend_full.c
    generated/guardian-legend_dispatch.c)
target_include_directories(GuardianLegendRecomp PRIVATE
    ${NESRECOMP_RUNNER_INCLUDE_DIRS} "${NESRECOMP_ROOT}/runner/src"
    "${CMAKE_CURRENT_SOURCE_DIR}" ${SDL2_INCLUDE_DIRS})
target_compile_definitions(GuardianLegendRecomp PRIVATE
    NESRECOMP_SPLIT_PARTS_EXTERNAL
    TGL_ROM_CRC32=0x${TGL_PRG_CRC32}u
    NESRECOMP_GAME_ID="guardian-legend-usa"
    NESRECOMP_GAME_PLAYERS=1
    NESRECOMP_GAME_ROM_CRC32="${TGL_PRG_CRC32}")
target_link_libraries(GuardianLegendRecomp PRIVATE ${SDL2_LIBRARIES})
if(WIN32)
    target_link_libraries(GuardianLegendRecomp PRIVATE ws2_32 dbghelp)
endif()
if(APPLE)
    # launcher.c sets ApplePressAndHoldEnabled via CFPreferences (accent-panel
    # opt-out for held keys); the Homebrew SDL2 dylib does not re-export it.
    target_link_libraries(GuardianLegendRecomp PRIVATE "-framework CoreFoundation")
endif()
if(TGL_LAUNCHER)
    include("${CMAKE_CURRENT_SOURCE_DIR}/recomp-ui/recomp_ui.cmake")
    recomp_target_launcher_ui(GuardianLegendRecomp CONSOLE nes)
endif()
include(CTest)
if(BUILD_TESTING)
    add_executable(tgl_mapper_test tests/mapper_test.c "${NESRECOMP_ROOT}/runner/src/mapper.c")
    target_include_directories(tgl_mapper_test PRIVATE "${NESRECOMP_ROOT}/runner/include")
    add_test(NAME uxrom COMMAND tgl_mapper_test)
    add_executable(tgl_raster_test tests/ppu_raster_test.c "${NESRECOMP_ROOT}/runner/src/ppu_dot.c")
    target_include_directories(tgl_raster_test PRIVATE "${NESRECOMP_ROOT}/runner/include")
    add_test(NAME ppu_raster_v COMMAND tgl_raster_test)
endif()
