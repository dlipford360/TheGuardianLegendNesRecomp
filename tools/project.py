#!/usr/bin/env python3
"""Local setup, verified ROM generation, and builds. No ROM is downloaded."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zlib

ROOT = Path(__file__).resolve().parents[1]
ROM_NAME = "Guardian Legend, The (U) [!].nes"
ROM_SHA256 = "5dd3fc3140066c3574b69557d3ebfe08fb0d110eaf8e309634ab1398eb0857b7"


def run(*args, cwd=ROOT):
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def rom_info(path):
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != ROM_SHA256:
        raise ValueError("ROM does not match the verified Guardian Legend USA image")
    if data[:16] != bytes.fromhex("4e45531a080021000000000000000000") or len(data) != 131088:
        raise ValueError("Unexpected cartridge header or size")
    return {"sha256": ROM_SHA256, "sha1": hashlib.sha1(data).hexdigest(),
            "size": len(data), "prg_crc32": f"{zlib.crc32(data[16:]):08x}",
            "mapper": 2, "prg_banks_16k": 8, "chr_ram_bytes": 8192,
            "mirroring": "vertical", "vectors": {"nmi": "dc75", "reset": "fede", "irq": "dcf3"}}


def patches(export=False):
    folder = ROOT / "patches"
    pins = json.loads((ROOT / "dependencies.json").read_text())
    for name in ("nesrecomp", "recomp-ui"):
        repo = ROOT / name
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        if actual != pins[name]:
            raise RuntimeError(f"{name}: expected {pins[name]}, found {actual}")
        patch = folder / f"{name}.patch"
        if export:
            diff = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=repo)
            if diff:
                folder.mkdir(exist_ok=True)
                patch.write_bytes(diff)
        elif patch.exists():
            applied = subprocess.run(["git", "apply", "--reverse", "--check", str(patch)],
                                     cwd=repo, capture_output=True)
            if applied.returncode:
                run("git", "apply", "--check", patch, cwd=repo)
                run("git", "apply", patch, cwd=repo)


def generate(rom):
    info = rom_info(rom)
    patches()
    out = ROOT / "build/recompiler"
    run("cmake", "-S", ROOT / "nesrecomp/recompiler", "-B", out, "-DCMAKE_BUILD_TYPE=Release")
    run("cmake", "--build", out, "--parallel", "4")
    exe = out / ("NESRecomp.exe" if os.name == "nt" else "NESRecomp")
    if not exe.exists() and os.name == "nt":
        exe = out / "Release/NESRecomp.exe"
    with (out / "generation.log").open("w") as log:
        subprocess.run([str(exe), str(rom), "--game", str(ROOT / "game.toml")],
                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    print(f"Generation log: {out / 'generation.log'}", flush=True)
    (ROOT / "generated/rom.json").write_text(json.dumps(info, indent=2) + "\n")
    (ROOT / "generated/rom.cmake").write_text(f'set(TGL_PRG_CRC32 "{info["prg_crc32"]}")\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "generate", "build", "rom-info", "export-patches"))
    parser.add_argument("--rom", type=Path, default=ROOT / ROM_NAME)
    parser.add_argument("--cosim", action="store_true", help="separate developer build without launcher")
    args = parser.parse_args()
    if args.command == "setup":
        run("git", "submodule", "update", "--init", "--recursive")
        patches()
    elif args.command == "export-patches":
        patches(export=True)
    elif args.command == "rom-info":
        print(json.dumps(rom_info(args.rom), indent=2))
    else:
        generate(args.rom.resolve())
        if args.command == "build":
            out = ROOT / ("build/cosim" if args.cosim else "build/game")
            trace = "ON" if args.cosim else "OFF"
            run("cmake", "-S", ROOT, "-B", out, "-DCMAKE_BUILD_TYPE=Release",
                f"-DTGL_LAUNCHER={'OFF' if args.cosim else 'ON'}",
                f"-DTGL_COSIM={trace}",
                f"-DNESRECOMP_ENABLE_TRACE={trace}",
                f"-DNESRECOMP_ENABLE_STACK_TRACKING={trace}",
                f"-DNESRECOMP_ENABLE_POSTMORTEM_RINGS={trace}")
            run("cmake", "--build", out, "--parallel", "4")


if __name__ == "__main__":
    main()
