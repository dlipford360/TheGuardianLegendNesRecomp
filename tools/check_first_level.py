#!/usr/bin/env python3
"""Replay two Start presses with fatal BRK diagnostics and retained evidence."""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from cosim import run_process, sha256
from project import ROOT, ROM_NAME, rom_info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--exe", type=Path, help="override executable for before/after regression checks")
    scenarios = parser.add_mutually_exclusive_group()
    scenarios.add_argument("--delayed", action="store_true", help="wait for title music to loop before starting")
    scenarios.add_argument("--combat", action="store_true", help="shoot and move during the opening corridor")
    scenarios.add_argument("--shoot", action="store_true",
                           help="strafe while holding fire until GAME OVER, then press START to retry")
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()
    rom = ROOT / ROM_NAME
    info = rom_info(rom)
    executable = (args.exe or ROOT / ("build/game" if args.windowed else "build/cosim") / "GuardianLegendRecomp").resolve()
    scenario = ("shoot" if args.shoot else "combat" if args.combat else "delayed" if args.delayed else "early")
    if args.timeout is None:
        # The shoot replay is ~6.4k frames: real-time windowed pacing needs the extra room.
        args.timeout = 240 if scenario == "shoot" else 90
    script = ROOT / "tests" / (f"first-level-{scenario}.script" if scenario != "early" else "first-level.script")
    output = ROOT / "build/first-level-runs"
    output.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=f"{scenario}-", dir=output))
    shutil.copy2(executable, output / executable.name)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NESRECOMP_", "NESREF_", "COSIM_"))}
    env.update(NESRECOMP_BRK="fatal", NESRECOMP_BAIL_TRACE="1")
    command = [str(output / executable.name), str(rom), "--script", str(script)]
    if not args.windowed:
        command += ["--smoke", "10000", "--smoke-output", str(output / "smoke.json")]
    result = run_process(command, output, env, args.timeout)
    stdout = (output / "stdout.log").read_text(errors="replace")
    stderr = (output / "stderr.log").read_text(errors="replace")
    completed = re.search(r"\[Script\] EXIT 0 at frame (\d+)", stdout)
    failures = []
    if result["status"] != "PASS":
        failures.append(result.get("reason", "process failed"))
    if completed is None:
        failures.append("script did not finish")
    if "[BRK]" in stdout + stderr or "FATAL: BRK" in stdout + stderr:
        failures.append("execution reached BRK")
    screenshots = [line.split()[1] for line in script.read_text().splitlines() if line.startswith("SCREENSHOT ")]
    for name in screenshots:
        if not (output / name).is_file():
            failures.append(f"missing screenshot {name}")
    bail_path = output / "bail_trace.log"
    sound_escapes = []
    if bail_path.exists():
        with bail_path.open() as stream:
            sound_escapes = [row for row in csv.DictReader(stream)
                             if row["caller_pc"] in {"$91F3", "$DCDF"}]
    if sound_escapes:
        failures.append("sound decoder/NMI stack escape")
    # The shoot scenario ends by retrying after GAME OVER; the historical black-screen
    # bug froze the display there, so require GAME OVER -> title -> new game on-screen.
    if scenario == "shoot" and not args.windowed and (output / "smoke.json").is_file():
        labels = {"5b26791d": "title", "ce983654": "game-over", "5079e9d5": "new-game",
                  "07673647": "black"}
        hashes = json.loads((output / "smoke.json").read_text())["frame_hashes"]
        sequence = []
        for frame in sorted(hashes, key=int):
            label = labels.get(hashes[frame], "play")
            if not sequence or sequence[-1] != label:
                sequence.append(label)
        retry = ["game-over", "title", "new-game"]
        rest, found = sequence, True
        for step in retry:
            if step not in rest:
                found = False
                break
            rest = rest[rest.index(step) + 1:]
        if not found:
            failures.append(f"missing retry sequence {retry} in {sequence}")
        if sequence and sequence[-1] == "black":
            failures.append("display stuck black at end of shoot replay")
        result["screen_sequence"] = sequence
    result.update(status="FAIL" if failures else "PASS", failures=failures,
                  script=str(script.relative_to(ROOT)), script_sha256=sha256(script),
                  executable_sha256=sha256(executable), rom=info,
                  frames=int(completed[1]) if completed else None,
                  screenshots=screenshots, sound_escapes=sound_escapes,
                  scope="script completion, fatal BRK and sound-stack checks; shoot also checks the "
                        "game-over/title/new-game screen sequence; inspect screenshots for presentation")
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"{result['status']}: {output / 'summary.json'}")
    for failure in failures:
        print(f"  {failure}")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
