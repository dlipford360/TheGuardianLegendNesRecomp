#!/usr/bin/env python3
"""Strict Guardian Legend co-simulation (standard library only).

Commands: selftest (all three live gates), gate1 (native determinism),
gate2 (oracle determinism), gate3 (live RAM/OAM faults), compare (independent).
Every invocation keeps a new directory under build/cosim-runs, even on failure.
PASS=0, FAIL=1, UNSUPPORTED=2. No input, freeze, automatic offsets or benign
masks are inherited from the environment. --offset means B[f + offset] vs A[f];
--start/--end/--stride select explicit checkpoints (default 1..N-1, stride 1).
Missing checkpoints fail; no state-based alignment or implicit intersection.

Trace contract (existing nes_cosim JSONL plus explicit termination):
  hash: {"f":1,"clk":...,"bclk":...,"chain":"16 hex digits","sub":{...}}
  delta: {"f":1,"adr":"0x0000","old":"0x00","val":"0x01"}
  cycle: {"f":1,"cyc":29781}
  Native hash rows are heartbeats, including duplicate NMI emissions. Native
  deltas carry forward only through the last hash frame, after smoke completion.
  Oracle NESREF_STATUS_FILE has {"type":"frame","f":F}
  for EVERY frame 1..N, followed by {"type":"complete","f":N,
  "requested_frames":N,"reason":"frame_limit"}, after all traces flushed.
Delta streams must initialize every meaningful byte in their first frame.
Unchanged frames carry forward ONLY through the final heartbeat. Dense hash
and cycle streams must cover every frame through their heartbeat. Duplicate
hash/cycle frames are last-wins for cross-engine sampling (multiple boundary
emits), counted in reports; self hash gates compare every duplicate emission.
Backwards frames are rejected. PPU layout: OAM [0,256), palette [256,288),
physical NT RAM [512,2560). The reserved gap is not compared.

Both engines must complete N frames (native host callbacks, oracle retro_run).
Native guest indices may exceed N. Repeats compare their entire equal domain,
including every hash emission; cross-engine comparison uses only the explicit
checkpoint domain. Cycles use bounded interval deltas, never absolute origins.
The ported nesref validates Mesen 0.9.9 MST v13 serialization at runtime.
Artifact hashes and source revisions/dirty diffs are recorded, not inferred
from agreement. No PPU canonicalization: all meaningful bytes compare raw.
Constant channels do not establish observability (reported separately from
repeat determinism). Independent CPU/mapper/CHR and other unextracted state
remain UNSUPPORTED; native hashes cover these only for repeat determinism.
Fault localization requires identical emission coverage through the injected
frame and no earlier divergence. Later guest coverage may differ as a live-fault
cascade, but both runs must still complete N and have internally valid traces.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass


ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = ROOT / "build/cosim-runs"
_spec = importlib.util.spec_from_file_location("tgl_project", ROOT / "tools/project.py")
_project = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_project)
rom_info = _project.rom_info
SUBS = ("cpu", "ram", "stack", "ppu_mem", "ppu_regs", "apu", "mapper", "chr", "sram", "openbus")
REGIONS = {"ram": (0, 0x800), "oam": (0, 0x100),
           "palette": (0x100, 0x120), "nt": (0x200, 0xA00)}
MESEN_REVISION = "0102910c39ad1a62bc3f784466f3f67ca9eae335"
UNEXTRACTED = ("cpu", "mapper", "chr", "ppu_regs", "apu", "sram", "openbus")


def load_rows(path):
    """Reject partial JSONL records, including a truncated final write."""
    rows = []
    with Path(path).open() as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith("\n") or not line.strip():
                raise TraceError(f"{path}:{line_number}: incomplete/empty JSONL record")
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise TraceError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return rows


class TraceError(ValueError):
    pass


def integer(value, label):
    if type(value) is not int or value < 0:
        raise TraceError(f"{label} must be a nonnegative integer")
    return value


def hexbyte(value, label, maximum=255):
    try:
        result = int(value, 16) if isinstance(value, str) else integer(value, label)
    except ValueError as exc:
        raise TraceError(f"invalid {label}: {value!r}") from exc
    if not 0 <= result <= maximum:
        raise TraceError(f"out of range {label}: {value!r}")
    return result


@dataclass
class Trace:
    frames: dict
    start: int
    end: int
    rows: int
    duplicates: int = 0
    emissions: dict = None

    def coverage(self):
        return {"start": self.start, "end": self.end,
                "frames": len(self.frames), "rows": self.rows,
                "duplicate_frames": self.duplicates}


def parse_trace(rows, kind):
    """Validate before reconstructing; never assume uninitialized RAM is zero."""
    if kind not in {"hash", "ram", "ppu", "cycle"}:
        raise TraceError(f"unknown trace channel {kind}")
    if not rows:
        raise TraceError("empty trace")
    groups = {}
    end = None
    previous = -1
    duplicates = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TraceError(f"row {index + 1} is not an object")
        f = integer(row.get("f"), "frame")
        if f < previous:
            raise TraceError(f"backwards frame {f} after {previous}")
        previous = f
        if "event" in row:
            if row != {"event": "end", "f": f} or index != len(rows) - 1:
                raise TraceError("unknown event or non-final end heartbeat")
            end = f
            continue
        if f in groups and kind in {"hash", "cycle"}:
            duplicates += 1
        groups.setdefault(f, []).append(row)
    if end is None:
        raise TraceError("missing end-run heartbeat")
    if not groups:
        raise TraceError("heartbeat without samples")
    start = min(groups)
    frames = {}
    if kind in {"ram", "ppu"}:
        size = 0x800 if kind == "ram" else 0xA00
        required = set(range(size)) if kind == "ram" else (
            set(range(0x120)) | set(range(0x200, 0xA00)))
        cur = bytearray(size)
        initialized = set()
        for f in range(start, end + 1):
            for row in groups.get(f, []):
                if not {"f", "adr", "val"} <= row.keys() or set(row) - {"f", "adr", "val", "old"}:
                    raise TraceError(f"unknown/incomplete {kind} row at {f}")
                a = hexbyte(row["adr"], "address", size - 1)
                v = hexbyte(row["val"], "value")
                if "old" in row:
                    old = hexbyte(row["old"], "old value")
                    if a in initialized and old != cur[a]:
                        raise TraceError(f"broken delta chain at frame {f}, address 0x{a:04x}")
                initialized.add(a)
                cur[a] = v
            if f == start and not required <= initialized:
                raise TraceError(f"incomplete initial {kind} snapshot: {len(required - initialized)} missing bytes")
            frames[f] = bytes(cur)
    else:
        if len(groups) != end - start + 1:
            missing = next(f for f in range(start, end + 1) if f not in groups)
            raise TraceError(f"missing {kind} frame {missing} (heartbeat ends at {end})")
        for f, samples in groups.items():
            for row in samples:
                if kind == "cycle":
                    if set(row) != {"f", "cyc"}:
                        raise TraceError(f"unknown/incomplete cycle row at {f}")
                    integer(row["cyc"], "cycle counter")
                else:
                    if set(row) != {"f", "clk", "bclk", "chain", "sub"}:
                        raise TraceError(f"unknown/incomplete hash row at {f}")
                    integer(row["clk"], "clock")
                    integer(row["bclk"], "boundary clock")
                    if not isinstance(row["sub"], dict) or set(row["sub"]) != set(SUBS):
                        raise TraceError(f"unknown/missing hash subsystem at {f}")
                    for value in [row["chain"], *row["sub"].values()]:
                        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", value):
                            raise TraceError(f"invalid 64-bit hash at {f}")
            frames[f] = samples[-1] if kind == "hash" else samples[-1]["cyc"]
    if kind == "hash":
        for clock in ("clk", "bclk"):
            emissions = [row[clock] for samples in groups.values() for row in samples]
            if any(b < a for a, b in zip(emissions, emissions[1:])):
                raise TraceError(f"nonmonotonic hash emission {clock}")
            values = [row[clock] for row in frames.values()]
            if any(b <= a for a, b in zip(values, values[1:])):
                raise TraceError(f"constant or nonmonotonic hash {clock}")
    return Trace(frames, start, end, len(rows) - 1, duplicates,
                 groups if kind == "hash" else None)


def oracle_completion(path, requested_frames):
    """The frontend publishes status only after flushing every requested channel."""
    rows = load_rows(path)
    if len(rows) != requested_frames + 1:
        raise TraceError("oracle status has incomplete frame coverage or no completion")
    for f, row in enumerate(rows[:-1], 1):
        if not isinstance(row, dict) or row != {"type": "frame", "f": f} or type(row.get("f")) is not int:
            raise TraceError(f"oracle status missing/invalid frame {f}")
    expected = {"type": "complete", "f": requested_frames,
                "requested_frames": requested_frames, "reason": "frame_limit"}
    if not isinstance(rows[-1], dict) or rows[-1] != expected or type(rows[-1].get("f")) is not int or type(rows[-1].get("requested_frames")) is not int:
        raise TraceError("oracle status lacks successful requested-frame completion")
    return requested_frames


def load_trace(path, kind, status_path=None, requested_frames=None, heartbeat=None):
    rows = load_rows(path)
    if not rows:
        raise TraceError(f"empty {kind} trace")
    if status_path is not None:
        end = oracle_completion(status_path, requested_frames)
    elif heartbeat is not None:
        end = heartbeat.end
    else:
        return parse_trace(rows, kind)
    if isinstance(rows[-1], dict) and rows[-1].get("event") == "end":
        if rows[-1].get("f") != end:
            raise TraceError("trace and status disagree on final frame")
    else:
        rows.append({"event": "end", "f": end})
    trace = parse_trace(rows, kind)
    if heartbeat is not None and trace.start != heartbeat.start:
        raise TraceError(f"{kind} initial snapshot does not match hash heartbeat start")
    return trace


def verdict(checks):
    states = [c["status"] for c in checks]
    return "FAIL" if any(s not in {"PASS", "UNSUPPORTED"} for s in states) else (
        "UNSUPPORTED" if not states or "UNSUPPORTED" in states else "PASS")


def compare(a, b, channel, offset=0, mask_stack=False,
            cycle_tolerance=2.0, min_frames=3, equal_cycle_origin=False,
            start=None, end=None, stride=1, determinism=False):
    """Strict declared-domain comparison. No intersection-only success."""
    if channel not in {"hash", "cycles", *REGIONS}:
        raise TraceError(f"unknown comparison channel {channel}")
    if min_frames < 2 or stride < 1 or not math.isfinite(cycle_tolerance) or cycle_tolerance < 0:
        raise TraceError("invalid comparison controls")
    explicit_window = start is not None or end is not None
    start = a.start if start is None else start
    end = a.end if end is None else end
    checkpoints = range(start, end + 1, stride)
    result = {"channel": channel, "status": "FAIL", "a": a.coverage(),
              "b": b.coverage(), "offset": offset,
              "compared_frames": 0, "first_mismatch": None,
              "masked_bytes_per_frame": 256 if mask_stack and channel == "ram" else 0,
              "excluded_a_frames": sum(f not in checkpoints for f in a.frames),
              "excluded_b_frames": sum(f - offset not in checkpoints for f in b.frames),
              "checkpoints": {"start": start, "end": end, "stride": stride},
              "scope": "repeat determinism" if determinism else "state comparison"}
    if len(checkpoints) < min_frames:
        result["reason"] = "insufficient comparison coverage"
        return result
    # Repeats use the whole domain. Only an explicit cross window excludes data.
    if not explicit_window and (start + offset != b.start or end + offset != b.end):
        result["reason"] = "unaligned frame domains (no automatic tail trimming)"
        first = min(a.start, b.start - offset) if start + offset != b.start else min(end, b.end - offset) + 1
        result["first_mismatch"] = {"frame": first,
                                    "kind": "coverage"}
        return result
    pairs = []
    for f in checkpoints:
        if f not in a.frames or f + offset not in b.frames:
            result["reason"] = "missing frame"
            result["first_mismatch"] = {"frame": f, "b_frame": f + offset, "kind": "coverage"}
            return result
        pairs.append((f, a.frames[f], b.frames[f + offset]))
    result["compared_frames"] = len(pairs)
    constants = []
    mismatches = 0
    if channel == "cycles":
        da = [pairs[i][1] - pairs[i - 1][1] for i in range(1, len(pairs))]
        db = [pairs[i][2] - pairs[i - 1][2] for i in range(1, len(pairs))]
        def stats(values):
            return {"mean": statistics.mean(values), "stddev": statistics.pstdev(values),
                    "min": min(values), "max": max(values), "count": len(values)}
        errors = [x - y for x, y in zip(da, db)]
        result.update(a_delta=stats(da), b_delta=stats(db), delta_error=stats(errors),
                      tolerance=cycle_tolerance, max_abs_delta_error=max(map(abs, errors)),
                      equal_cycle_origin=equal_cycle_origin)
        if min(da + db) <= 0:
            result["reason"] = "constant or nonmonotonic cycle counter"
            i = next(i for i, (x, y) in enumerate(zip(da, db)) if min(x, y) <= 0)
            result["first_mismatch"] = {"frame": pairs[i + 1][0], "kind": "invalid_cycle_delta", "a": da[i], "b": db[i]}
            return result
        # Reject bogus units/counters without using absolute counter alignment.
        if any(not 20000 <= statistics.mean(values) / stride <= 40000 for values in (da, db)):
            result["reason"] = "implausible NTSC CPU cycles per guest frame (expected mean 20000..40000)"
            return result
        if equal_cycle_origin and pairs[0][1] != pairs[0][2]:
            mismatches += 1
            result["first_mismatch"] = {"frame": pairs[0][0], "kind": "cycle_origin",
                                        "a": pairs[0][1], "b": pairs[0][2]}
        for i, error in enumerate(errors):
            if abs(error) > cycle_tolerance:
                mismatches += 1
                if result["first_mismatch"] is None:
                    result["first_mismatch"] = {"frame": pairs[i + 1][0], "from_frame": pairs[i][0], "kind": "cycle_delta",
                                                "a": da[i], "b": db[i], "error": error}
    elif channel == "hash":
        for sub in SUBS:
            if any(len({r[side]["sub"][sub] for r in pairs}) == 1 for side in (1, 2)):
                constants.append(sub)
        for f, ar, br in pairs:
            aa = a.emissions[f] if a.emissions else [ar]
            bb = b.emissions[f + offset] if b.emissions else [br]
            if len(aa) != len(bb):
                mismatches += 1
                if result["first_mismatch"] is None:
                    result["first_mismatch"] = {"frame": f, "kind": "emission_coverage",
                                                "a_emissions": len(aa), "b_emissions": len(bb)}
            for emission, (ar, br) in enumerate(zip(aa, bb)):
                subs = [s for s in SUBS if ar["sub"][s] != br["sub"][s]]
                fields = [s for s in ("chain", "clk", "bclk") if ar[s] != br[s]]
                if subs or fields:
                    mismatches += 1
                    if result["first_mismatch"] is None:
                        result["first_mismatch"] = {"frame": f, "b_frame": f + offset,
                                                    "emission": emission, "kind": "hash",
                                                    "subsystems": subs, "fields": fields}
    else:
        lo, hi = REGIONS[channel]
        indices = [i for i in range(lo, hi)
                   if not (channel == "ram" and mask_stack and 0x100 <= i < 0x200)]
        for side in (1, 2):
            if any(len(row[side]) < hi for row in pairs):
                raise TraceError(f"short {channel} image")
            if len({bytes(row[side][i] for i in indices) for row in pairs}) == 1:
                constants.append("a" if side == 1 else "b")
        for f, ar, br in pairs:
            for i in indices:
                if ar[i] != br[i]:
                    mismatches += 1
                    if result["first_mismatch"] is None:
                        result["first_mismatch"] = {"frame": f, "b_frame": f + offset,
                                                    "kind": "byte", "address": f"0x{i:04x}",
                                                    "region_offset": i - lo, "a": ar[i], "b": br[i]}
        result["compared_bytes"] = len(indices) * len(pairs)
    result["mismatch_count"] = mismatches
    result["constant_channels"] = constants
    unobservable = bool(constants) and not determinism
    if channel == "hash" and len(constants) == len(SUBS):
        unobservable = True
    result["status"] = "FAIL" if mismatches else "UNSUPPORTED" if unobservable else "PASS"
    if constants:
        result["observability"] = "UNSUPPORTED for constant channels; equality alone is not liveness"
    return result


def fault_result(clean, injected, frame, region):
    """Localize a fault in traces already validated after successful N-frame runs."""
    expected = {"ram": "ram", "oam": "ppu_mem"}
    if region not in expected:
        raise TraceError(f"unknown fault region {region}")
    # A live fault may change future guest/host frame ratios. Compare every
    # emission through injection, not an intersection or the post-fault tail.
    start = min(clean.start, injected.start)
    result = compare(clean, injected, "hash", start=start, end=frame, min_frames=2)
    first = result["first_mismatch"]
    prefix = range(start, frame + 1)
    coverage_equal = (clean.start == injected.start
                      and all(f in clean.frames and f in injected.frames
                              and len(clean.emissions[f]) == len(injected.emissions[f]) for f in prefix))
    localized = (coverage_equal
                 and first is not None and first["kind"] == "hash"
                 and first["frame"] == frame
                 and first["subsystems"] == [expected[region]]
                 and not (set(first["fields"]) - {"chain"}))
    tails = [{f: len(rows) for f, rows in trace.emissions.items() if f > frame}
             for trace in (clean, injected)]
    result.update(channel=f"fault_{region}", expected_frame=frame,
                  expected_subsystem=expected[region], status="PASS" if localized else "FAIL",
                  scope="fault localization prefix; not full-run equivalence",
                  prefix_coverage_equal=coverage_equal,
                  post_injection_coverage={"a_end": clean.end, "b_end": injected.end,
                                           "a_frames": len(tails[0]), "b_frames": len(tails[1]),
                                           "a_emissions": sum(tails[0].values()),
                                           "b_emissions": sum(tails[1].values()),
                                           "differs": tails[0] != tails[1],
                                           "policy": "expected cascade allowed only after successful N-frame completion and whole-trace validation"})
    result["reason"] = "live fault localized; post-injection cascade excluded" if localized else "fault absent, early, late, wrong subsystem, or unequal prefix coverage"
    return result


def sha256(path):
    with open(path, "rb") as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def engine_revision(directory):
    """Record staged + unstaged differences from HEAD, plus untracked contents."""
    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=directory, stderr=subprocess.PIPE, timeout=30)
    revision = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD")
    untracked = {}
    for name in git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0"):
        if name:
            untracked[name] = sha256(Path(directory) / name)
    return {"path": str(directory), "revision": revision,
            "dirty": bool(git("status", "--porcelain")),
            "diff_sha256": hashlib.sha256(diff).hexdigest(), "untracked": untracked}


def run_process(command, directory, env, timeout):
    """Retain both logs and kill the process group on POSIX timeouts."""
    directory = Path(directory)
    result = {"command": list(map(str, command)), "cwd": str(directory),
              "timeout_seconds": timeout, "status": "FAIL", "returncode": None,
              "stdout": str(directory / "stdout.log"), "stderr": str(directory / "stderr.log")}
    start = time.monotonic()
    with open(result["stdout"], "wb") as stdout, open(result["stderr"], "wb") as stderr:
        try:
            proc = subprocess.Popen(command, cwd=directory, env=env, stdout=stdout, stderr=stderr,
                                    start_new_session=(os.name == "posix"))
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                result["reason"] = "subprocess timeout"
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait()
            result["returncode"] = proc.returncode
            if proc.returncode == 0 and "reason" not in result:
                result["status"] = "PASS"
            else:
                result.setdefault("reason", "nonzero subprocess exit")
        except OSError as exc:
            result["reason"] = str(exc)
    result["elapsed_seconds"] = time.monotonic() - start
    return result


def run_engine(args, root, tag, engine, runs, inject=None):
    directory = root / tag
    directory.mkdir()
    original = Path(args.exe if engine == "recomp" else args.nesref).resolve()
    # Never launch the original: SDL_GetBasePath-based saves also need isolation.
    executable = directory / original.name
    shutil.copy2(original, executable)
    rom = directory / "game.nes"
    shutil.copy2(args.rom, rom)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("NESRECOMP_", "NESREF_", "COSIM_"))}
    env.update(HOME=str(directory), XDG_CONFIG_HOME=str(directory / "config"),
               XDG_DATA_HOME=str(directory / "data"), XDG_CACHE_HOME=str(directory / "cache"),
               SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
    paths = {s: directory / f"{s}.jsonl" for s in ("hash", "ram", "ppu", "cycle")}
    if engine == "recomp":
        env.update(NESRECOMP_COSIM_HASH=str(paths["hash"]),
                   NESRECOMP_WRAM_TRACE=str(paths["ram"]), NESRECOMP_PPUMEM_TRACE=str(paths["ppu"]))
        if inject:
            env["NESRECOMP_COSIM_INJECT"] = inject
        command = [str(executable), str(rom), "--smoke", str(args.frames),
                   "--smoke-output", str(directory / "smoke.json")]
    else:
        core = directory / ("mesen_libretro" + Path(args.core).suffix)
        shutil.copy2(args.core, core)
        env.update(NESREF_FRAMES=str(args.frames), NESREF_HEADLESS="1",
                   NESREF_TRACE_FILE=str(paths["ram"]), NESREF_PPU_FILE=str(paths["ppu"]),
                   NESREF_CYCLE_FILE=str(paths["cycle"]), NESREF_STATUS_FILE=str(directory / "status.jsonl"))
        command = [str(executable), str(core), str(rom)]
    record = run_process(command, directory, env, args.timeout)
    record.update(tag=tag, engine=engine, original_executable=str(original),
                  requested_frames=args.frames if engine == "recomp" else int(env["NESREF_FRAMES"]),
                  requested_clock="smoke_callbacks" if engine == "recomp" else "oracle_guest_frames",
                  executable_sha256=sha256(executable), rom_sha256=sha256(rom),
                  trace_paths={k: str(v) for k, v in paths.items()},
                  trace_env={k: v for k, v in env.items() if k.startswith(("NESRECOMP_", "NESREF_"))})
    runs.append(record)
    if engine == "recomp":
        record["smoke_path"] = str(directory / "smoke.json")
    if engine == "oracle":
        record["core_sha256"] = sha256(core)
    if record["status"] != "PASS":
        raise TraceError(f"{tag}: {record.get('reason')}")
    if engine == "oracle":
        record["status_path"] = env["NESREF_STATUS_FILE"]
        try:
            record["completed_guest_frame"] = oracle_completion(env["NESREF_STATUS_FILE"], int(env["NESREF_FRAMES"]))
        except (OSError, ValueError) as exc:
            record.update(status="FAIL", reason=str(exc))
            raise TraceError(f"{tag}: {exc}") from exc
    if engine == "recomp":
        try:
            smoke = json.loads((directory / "smoke.json").read_text())
            if not isinstance(smoke, dict):
                raise TraceError("smoke result must be an object")
            reached = integer(smoke.get("frames_run"), "frames_run")
            record["smoke_frames_run"] = reached
            if reached != args.frames:
                raise TraceError(f"smoke exited early or overran: {reached} != {args.frames}")
            if integer(smoke.get("dispatch_miss_count"), "dispatch_miss_count") != 0:
                raise TraceError("smoke reported dispatch misses")
        except (OSError, ValueError) as exc:
            record.update(status="FAIL", reason=f"missing/invalid smoke completion: {exc}")
            raise TraceError(record["reason"]) from exc
    return paths


def checked_trace(path, kind, args, engine):
    if engine == "oracle":
        trace = load_trace(path, kind, Path(path).parent / "status.jsonl", args.frames)
    elif kind == "hash":
        rows = load_rows(path)
        if not rows:
            raise TraceError("empty hash trace")
        if isinstance(rows[-1], dict) and "event" not in rows[-1]:
            rows.append({"event": "end", "f": rows[-1].get("f")})
        trace = parse_trace(rows, kind)
    else:
        heartbeat = checked_trace(Path(path).parent / "hash.jsonl", "hash", args, engine)
        trace = load_trace(path, kind, heartbeat=heartbeat)
    if trace.start not in ((1,) if engine == "oracle" else (0, 1)):
        raise TraceError(f"{kind} starts at {trace.start}; boot coverage missing")
    if trace.end < args.frames - 1:
        raise TraceError(f"{kind} ends at guest frame {trace.end}; need coverage through {args.frames - 1}")
    if len(trace.frames) < 3:
        raise TraceError(f"{kind} has insufficient guest coverage")
    if engine == "oracle" and trace.end != args.frames:
        raise TraceError(f"oracle {kind} ends at {trace.end}, expected {args.frames}")
    return trace


def compare_independent(ap, bp, args, engines):
    checks = []
    traces = {}
    for side, paths, engine in zip(("a", "b"), (ap, bp), engines):
        for kind in ("ram", "ppu", "hash" if engine == "recomp" else "cycle"):
            try:
                trace = checked_trace(paths[kind], kind, args, engine)
                if kind == "hash":
                    trace = Trace({f: row["bclk"] for f, row in trace.frames.items()},
                                  trace.start, trace.end, trace.rows, trace.duplicates)
                traces[side, "cycle" if kind == "hash" else kind] = trace
            except (OSError, ValueError) as exc:
                checks.append({"channel": f"{side}_{kind}", "status": "FAIL", "reason": str(exc)})
        available = [v for (s, _), v in traces.items() if s == side]
        if len({(t.start, t.end) for t in available}) > 1:
            checks.append({"channel": f"{side}_coverage", "status": "FAIL",
                           "reason": "channels disagree on initial/final guest frame"})
    for channel in ("ram", "oam", "palette", "nt", "cycles"):
        kind = "ram" if channel == "ram" else "cycle" if channel == "cycles" else "ppu"
        if ("a", kind) not in traces or ("b", kind) not in traces:
            continue
        repeat = engines == ("oracle", "oracle")
        controls = {} if repeat else {"start": args.start, "end": args.end,
                                      "stride": args.stride, "offset": args.offset,
                                      "mask_stack": args.mask_stack}
        result = compare(traces["a", kind], traces["b", kind], channel,
                         cycle_tolerance=0 if repeat else args.cycle_tolerance,
                         equal_cycle_origin=repeat, determinism=repeat, **controls)
        checks.append(result)
    if engines != ("oracle", "oracle"):
        for channel in UNEXTRACTED:
            checks.append({"channel": channel, "status": "UNSUPPORTED",
                           "reason": "no independent oracle extraction; native hash is determinism-only"})
    else:
        # Oracle has no full-state hash. Repeat every emitted row as well as the
        # reconstructed images, so duplicate/transient emissions cannot hide.
        for kind in ("ram", "ppu", "cycle"):
            try:
                aa, bb = load_rows(ap[kind]), load_rows(bp[kind])
                checks.append({"channel": f"oracle_{kind}_emissions",
                               "status": "PASS" if aa == bb and aa else "FAIL",
                               "a_rows": len(aa), "b_rows": len(bb)})
            except (OSError, ValueError) as exc:
                checks.append({"channel": f"oracle_{kind}_emissions", "status": "FAIL", "reason": str(exc)})
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("selftest", "gate1", "gate2", "gate3", "compare"))
    parser.add_argument("--exe", default=str(ROOT / "build/cosim/GuardianLegendRecomp"),
                        help="native executable (copied into each private run)")
    parser.add_argument("--rom", default=str(ROOT / _project.ROM_NAME))
    parser.add_argument("--nesref", default=str(ROOT / "build/oracle/nesref"))
    parser.add_argument("--core", default=str(ROOT / "build/oracle/mesen_libretro.dylib"))
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--out", default=str(RUNS_ROOT), help="retained parent, must be under build/cosim-runs")
    parser.add_argument("--offset", type=int, default=0, help="compare A[f] to B[f+offset], no search")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, help="last cross-engine checkpoint bound, default N-1")
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--mask-stack", action="store_true", help="explicitly ignore RAM $0100-$01ff in A/B only")
    parser.add_argument("--cycle-tolerance", type=float, default=2.0,
                        help="maximum cycle DELTA error per checkpoint interval (not scaled by stride)")
    parser.add_argument("--fault-frame", type=int, default=60, help="injected guest frame (default 60)")
    args = parser.parse_args(argv)
    args.end = args.frames - 1 if args.end is None else args.end
    if args.frames < 4 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("need frames >= 4 and finite positive timeout")
    if args.start < 1 or args.end < args.start or args.stride < 1 or len(range(args.start, args.end + 1, args.stride)) < 3:
        parser.error("need at least three checkpoints, start >= 1, end >= start, stride >= 1")
    if args.fault_frame < 1:
        parser.error("fault frame must be positive")
    if not math.isfinite(args.cycle_tolerance) or args.cycle_tolerance < 0:
        parser.error("cycle tolerance must be finite and nonnegative")
    if args.command != "compare" and (args.offset or args.start != 1 or args.end != args.frames - 1
                                     or args.stride != 1 or args.mask_stack or args.cycle_tolerance != 2):
        parser.error("offset, start, end, stride, mask-stack and cycle tolerance apply only to compare")
    required = ["rom"]
    if args.command in {"selftest", "gate1", "gate3", "compare"}:
        required.append("exe")
    if args.command in {"selftest", "gate2", "compare"}:
        required.extend(("nesref", "core"))
    parent = Path(args.out).resolve()
    if not parent.is_relative_to(RUNS_ROOT.resolve()):
        parser.error("--out must remain under build/cosim-runs (retained, ignored isolation)")
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=args.command + "-", dir=parent))
    report = {"schema": 2, "command": args.command, "status": "FAIL", "directory": str(root),
              "controls": vars(args).copy(), "runs": [], "checks": [], "gates": {},
              "scope": "neutral input; repeat determinism is not independent equivalence; hash excludes live PC/host state",
              "canonicalization": "none; raw RAM/OAM/palette/physical 2KB NT, reserved PPU gap excluded",
              "oracle_contract": {"core_revision": MESEN_REVISION, "serialization": "Mesen 0.9.9 MST v13",
                                  "validation": "ported nesref validates layout/array sizes and serialized RAM against SYSTEM_RAM"},
              "independent_support": {**{c: "raw bytes" for c in REGIONS}, "cycles": "interval delta",
                                      **{c: "UNSUPPORTED" for c in UNEXTRACTED}},
              "provenance_caveat": "source revision/diff is recorded at invocation; artifact hashes pin binaries, not proof of source-to-build correspondence",
              "coverage_contract": "native hash heartbeat + smoke completion; oracle status + completion; initial full deltas then carry-forward",
              "harness_sha256": sha256(__file__)}
    try:
        report["rom"] = rom_info(args.rom)
        report["engine"] = engine_revision(ROOT / "nesrecomp")
        mesen_source = ROOT / "build/oracle-src/Mesen"
        if "core" in required and mesen_source.exists():
            report["oracle_source"] = engine_revision(mesen_source)
            if report["oracle_source"]["revision"] != MESEN_REVISION or report["oracle_source"]["dirty"]:
                raise TraceError("oracle source is not the clean pinned Mesen revision")
        # Freeze artifacts once, then make each execution's own private copies.
        # Concurrent engine rebuilds cannot mix A/A binaries within this report.
        for name in required:
            original = Path(getattr(args, name)).resolve()
            folder = root / "artifacts" / name
            folder.mkdir(parents=True)
            snapshot = folder / original.name
            identity = sha256(original)
            shutil.copy2(original, snapshot)
            if sha256(snapshot) != identity:
                raise TraceError(f"{name} changed while copying artifact")
            report.setdefault("artifacts", {})[name] = {"path": str(original), "sha256": identity,
                                                        "snapshot": str(snapshot)}
            setattr(args, name, str(snapshot))
        rom_info(args.rom)
        def run(tag, engine, inject=None):
            paths = run_engine(args, root, tag, engine, report["runs"], inject)
            try:
                kinds = ("hash", "ram", "ppu") if engine == "recomp" else ("ram", "ppu", "cycle")
                report["runs"][-1]["coverage"] = {
                    kind: checked_trace(paths[kind], kind, args, engine).coverage() for kind in kinds}
            except (OSError, ValueError) as exc:
                report["runs"][-1].update(status="FAIL", reason=str(exc))
                raise
            return paths

        clean = None
        floor = None
        gates = ("gate1", "gate2", "gate3") if args.command == "selftest" else (args.command,)
        for gate in gates:
            checks = []
            try:
                if gate in {"gate1", "gate3"}:
                    if gate == "gate1" or args.command == "gate3":
                        clean_path = run("native-a", "recomp")
                        repeat_path = run("native-b", "recomp")
                        clean = checked_trace(clean_path["hash"], "hash", args, "recomp")
                        repeat = checked_trace(repeat_path["hash"], "hash", args, "recomp")
                        floor = compare(clean, repeat, "hash", determinism=True)
                        checks.append(floor)
                    if gate == "gate3":
                        if floor is None or floor["status"] != "PASS":
                            raise TraceError("fault test requires a passing native repeat floor")
                        frame = args.fault_frame
                        if not clean.start < frame < clean.end:
                            raise TraceError("fault frame must be inside clean guest coverage, not a smoke tick")
                        for region, index in (("ram", "0x300"), ("oam", "7")):
                            fault_path = run("fault-" + region, "recomp", f"{frame}:{region}:{index}:ff")
                            fault = checked_trace(fault_path["hash"], "hash", args, "recomp")
                            checks.append(fault_result(clean, fault, frame, region))
                elif gate == "gate2":
                    ap, bp = run("oracle-a", "oracle"), run("oracle-b", "oracle")
                    checks.extend(compare_independent(ap, bp, args, ("oracle", "oracle")))
                else:
                    ap, bp = run("native-a", "recomp"), run("oracle-a", "oracle")
                    checks.extend(compare_independent(ap, bp, args, ("recomp", "oracle")))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                checks.append({"channel": "harness", "status": "FAIL", "reason": str(exc)})
            for check in checks:
                check["gate"] = gate
            report["checks"].extend(checks)
            report["gates"][gate] = verdict(checks)
        report["status"] = verdict(report["checks"])
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        report["checks"].append({"channel": "harness", "status": "FAIL", "reason": str(exc)})
        report["status"] = "FAIL"
    for record in report["runs"]:
        record["trace_sha256"] = {kind: sha256(path) for kind, path in record.get("trace_paths", {}).items()
                                  if Path(path).is_file()}
    (root / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    for gate, status in report["gates"].items():
        print(f"{gate}: {status}")
    for check in report["checks"]:
        label = f"{check.get('gate', 'harness')}/{check['channel']}: {check['status']}"
        if check.get("first_mismatch") is not None:
            print(f"{label}; first mismatch: {json.dumps(check['first_mismatch'])}")
        elif check["status"] == "FAIL":
            print(f"{label}; {check.get('reason', 'check failed')}")
    print(f"{report['status']}: {root / 'summary.json'}")
    return {"PASS": 0, "FAIL": 1, "UNSUPPORTED": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
