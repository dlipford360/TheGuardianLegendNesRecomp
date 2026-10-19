"""Run with python3 -m unittest discover -s tests -p test_cosim.py -v."""

import importlib.util
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tgl_cosim", ROOT / "tools/cosim.py")
cosim = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cosim
spec.loader.exec_module(cosim)


def hashes(count=4, start=1):
    return [{"f": f, "clk": f * 30000 + 50, "bclk": f * 30000,
             "chain": f"{f:016x}", "sub": {s: f"{f:016x}" for s in cosim.SUBS}}
            for f in range(start, start + count)] + [{"event": "end", "f": start + count - 1}]


def deltas(kind="ram", end=4):
    size = 0x800 if kind == "ram" else 0xA00
    return ([{"f": 1, "adr": hex(a), "old": "0x00", "val": "0x00"} for a in range(size)]
            + [{"f": 3, "adr": "0x07", "old": "0x00", "val": "0xff"},
               {"event": "end", "f": end}])


def write_rows(path, rows):
    Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows))


class TraceTests(unittest.TestCase):
    def test_empty(self):
        with self.assertRaisesRegex(cosim.TraceError, "empty"):
            cosim.parse_trace([], "ram")

    def test_heartbeat_only(self):
        with self.assertRaisesRegex(cosim.TraceError, "without samples"):
            cosim.parse_trace([{"event": "end", "f": 5}], "ram")

    def test_missing_heartbeat(self):
        for kind, rows in (("ram", deltas()[:-1]), ("hash", hashes()[:-1])):
            with self.subTest(kind=kind), self.assertRaisesRegex(cosim.TraceError, "heartbeat"):
                cosim.parse_trace(rows, kind)

    def test_initial_full_snapshot_required(self):
        rows = deltas()
        del rows[123]
        with self.assertRaisesRegex(cosim.TraceError, "initial.*1 missing"):
            cosim.parse_trace(rows, "ram")

    def test_sparse_carry_forward_and_final_heartbeat(self):
        trace = cosim.parse_trace(deltas(), "ram")
        self.assertEqual(list(trace.frames), [1, 2, 3, 4])
        self.assertEqual(trace.frames[1], trace.frames[2])
        self.assertEqual(trace.frames[3], trace.frames[4])
        self.assertEqual(trace.frames[4][7], 255)

    def test_broken_old_value(self):
        rows = deltas()
        rows[-2]["old"] = "0x01"
        with self.assertRaisesRegex(cosim.TraceError, "broken delta"):
            cosim.parse_trace(rows, "ram")

    def test_ppu_gap_not_required_but_nt_is(self):
        rows = [r for r in deltas("ppu") if "adr" not in r or not 0x120 <= int(r["adr"], 16) < 0x200]
        self.assertEqual(cosim.parse_trace(rows, "ppu").end, 4)
        rows = [r for r in rows if r.get("adr") != "0x200"]
        with self.assertRaisesRegex(cosim.TraceError, "initial"):
            cosim.parse_trace(rows, "ppu")

    def test_dense_missing_middle_and_tail(self):
        for index in (1, 3):
            rows = hashes()
            del rows[index]
            with self.subTest(index=index), self.assertRaisesRegex(cosim.TraceError, "missing hash frame"):
                cosim.parse_trace(rows, "hash")

    def test_backwards_and_unknown_events(self):
        rows = hashes()
        rows[1]["f"] = 0
        with self.assertRaisesRegex(cosim.TraceError, "backwards"):
            cosim.parse_trace(rows, "hash")
        with self.assertRaisesRegex(cosim.TraceError, "unknown event"):
            cosim.parse_trace([{"f": 1, "event": "maybe_end"}], "ram")

    def test_duplicate_dense_last_wins_counted(self):
        rows = hashes()
        duplicate = dict(rows[0], clk=30099)
        rows.insert(1, duplicate)
        trace = cosim.parse_trace(rows, "hash")
        self.assertEqual(trace.duplicates, 1)
        self.assertEqual(trace.frames[1]["clk"], 30099)

    def test_invalid_channels_and_values(self):
        for value in (True, -1, "3", None):
            rows = hashes()
            rows[0]["f"] = value
            with self.subTest(value=value), self.assertRaises(cosim.TraceError):
                cosim.parse_trace(rows, "hash")
        for mutation in (lambda r: r[0]["sub"].pop("ram"),
                         lambda r: r[0]["sub"].update(unknown="0" * 16),
                         lambda r: r[0].update(chain="garbage")):
            rows = hashes()
            mutation(rows)
            with self.assertRaises(cosim.TraceError):
                cosim.parse_trace(rows, "hash")
        with self.assertRaises(cosim.TraceError):
            cosim.parse_trace(hashes(), "unknown")


class CompareTests(unittest.TestCase):
    def test_equal_hashes(self):
        trace = cosim.parse_trace(hashes(), "hash")
        self.assertEqual(cosim.compare(trace, trace, "hash")["status"], "PASS")

    def test_no_overlap_and_equal_length_shifted_ids(self):
        a = cosim.parse_trace(hashes(), "hash")
        for start in (2, 20):
            b = cosim.parse_trace(hashes(start=start), "hash")
            result = cosim.compare(a, b, "hash")
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["first_mismatch"]["kind"], "coverage")

    def test_truncation_both_directions(self):
        a = cosim.parse_trace(hashes(), "hash")
        b = cosim.parse_trace(hashes(count=3), "hash")
        self.assertEqual(cosim.compare(a, b, "hash")["status"], "FAIL")
        self.assertEqual(cosim.compare(b, a, "hash")["status"], "FAIL")

    def test_extra_prefix_not_silently_skipped(self):
        a = cosim.parse_trace(hashes(), "hash")
        b = cosim.parse_trace(hashes(count=5, start=0), "hash")
        self.assertEqual(cosim.compare(a, b, "hash")["status"], "FAIL")

    def test_duplicate_hashes_do_not_hide_transient_mismatch(self):
        rows = hashes()
        rows.insert(1, dict(rows[0]))
        a = cosim.parse_trace(rows, "hash")
        rows = hashes()
        rows.insert(1, dict(rows[0]))
        rows[0] = {**rows[0], "sub": {**rows[0]["sub"], "ram": "f" * 16}}
        b = cosim.parse_trace(rows, "hash")
        result = cosim.compare(a, b, "hash")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["first_mismatch"]["emission"], 0)
        c = cosim.parse_trace(hashes(), "hash")
        self.assertEqual(cosim.compare(a, c, "hash")["first_mismatch"]["kind"], "emission_coverage")

    def test_comparator_missing_middle_even_with_overlap(self):
        a = cosim.parse_trace(hashes(), "hash")
        b = cosim.parse_trace(hashes(), "hash")
        del b.frames[2]
        result = cosim.compare(a, b, "hash")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["first_mismatch"]["frame"], 2)

    def test_subsystem_mismatch_even_when_chain_equal(self):
        a = cosim.parse_trace(hashes(), "hash")
        rows = hashes()
        rows[1]["sub"]["ram"] = "f" * 16
        b = cosim.parse_trace(rows, "hash")
        result = cosim.compare(a, b, "hash")
        self.assertEqual(result["first_mismatch"]["frame"], 2)
        self.assertEqual(result["first_mismatch"]["subsystems"], ["ram"])
        self.assertEqual(result["status"], "FAIL")

    def test_constant_subsystem_not_pass(self):
        rows = hashes()
        for row in rows[:-1]:
            row["sub"]["ram"] = "0" * 16
        trace = cosim.parse_trace(rows, "hash")
        result = cosim.compare(trace, trace, "hash")
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertEqual(result["constant_channels"], ["ram"])
        repeat = cosim.compare(trace, trace, "hash", determinism=True)
        self.assertEqual(repeat["status"], "PASS")
        self.assertIn("UNSUPPORTED", repeat["observability"])
        for row in rows[:-1]:
            row["sub"] = {s: "0" * 16 for s in cosim.SUBS}
        trace = cosim.parse_trace(rows, "hash")
        self.assertEqual(cosim.compare(trace, trace, "hash", determinism=True)["status"], "UNSUPPORTED")

    def test_sparse_matches_dense_and_localizes(self):
        sparse = cosim.parse_trace(deltas(), "ram")
        rows = deltas()
        rows.insert(-1, {"f": 4, "adr": "0x07", "old": "0xff", "val": "0xff"})
        dense = cosim.parse_trace(rows, "ram")
        self.assertEqual(cosim.compare(sparse, dense, "ram")["status"], "PASS")
        rows[-2]["val"] = "0x01"
        faulty = cosim.parse_trace(rows, "ram")
        mismatch = cosim.compare(sparse, faulty, "ram")["first_mismatch"]
        self.assertEqual((mismatch["frame"], mismatch["address"]), (4, "0x0007"))

    def test_stack_not_masked_by_default(self):
        a = cosim.parse_trace(deltas(), "ram")
        rows = deltas()
        rows.insert(-1, {"f": 4, "adr": "0x100", "val": "0xff"})
        b = cosim.parse_trace(rows, "ram")
        self.assertEqual(cosim.compare(a, b, "ram")["status"], "FAIL")
        result = cosim.compare(a, b, "ram", mask_stack=True)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["masked_bytes_per_frame"], 256)

    def test_palette_no_automatic_mirror_mask(self):
        a = cosim.parse_trace(deltas("ppu"), "ppu")
        rows = deltas("ppu")
        rows.insert(-1, {"f": 4, "adr": "0x110", "val": "0x3f"})
        b = cosim.parse_trace(rows, "ppu")
        result = cosim.compare(a, b, "palette")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["first_mismatch"]["address"], "0x0110")

    def test_constants_and_insufficient_window(self):
        a = cosim.parse_trace(deltas("ppu"), "ppu")
        self.assertEqual(cosim.compare(a, a, "nt")["status"], "UNSUPPORTED")
        self.assertEqual(cosim.compare(a, a, "oam", start=4)["status"], "FAIL")

    def test_explicit_offset_no_optimization(self):
        a = cosim.parse_trace(deltas(), "ram")
        rows = deltas()
        for row in rows:
            row["f"] += 2
        b = cosim.parse_trace(rows, "ram")
        self.assertEqual(cosim.compare(a, b, "ram")["status"], "FAIL")
        self.assertEqual(cosim.compare(a, b, "ram", offset=2)["status"], "PASS")

    def test_explicit_checkpoints_not_intersection_or_observed_tail(self):
        a = cosim.parse_trace(deltas(end=8), "ram")
        b = cosim.parse_trace(deltas(end=6), "ram")
        self.assertEqual(cosim.compare(a, b, "ram")["status"], "FAIL")
        result = cosim.compare(a, b, "ram", start=1, end=5, stride=2)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["compared_frames"], 3)
        self.assertEqual(result["excluded_a_frames"], 5)
        self.assertEqual(result["excluded_b_frames"], 3)
        del b.frames[3]
        result = cosim.compare(a, b, "ram", start=1, end=5, stride=2)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["first_mismatch"]["frame"], 3)
        result = cosim.compare(a, b, "ram", start=1, end=8)
        self.assertEqual(result["status"], "FAIL")

    def test_missing_unsampled_dense_frame_is_still_invalid(self):
        rows = hashes(count=6)
        del rows[1]
        with self.assertRaisesRegex(cosim.TraceError, "missing hash frame 2"):
            cosim.parse_trace(rows, "hash")

    def test_cycle_deltas_not_absolute_offsets(self):
        def trace(values):
            return cosim.parse_trace([{"f": i, "cyc": value} for i, value in enumerate(values, 1)]
                                     + [{"event": "end", "f": len(values)}], "cycle")
        a = trace([30000, 60000, 90000, 120000])
        b = trace([30100, 60101, 90100, 120101])
        result = cosim.compare(a, b, "cycles", cycle_tolerance=1)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["a_delta"]["mean"], 30000)
        self.assertEqual(cosim.compare(a, b, "cycles", cycle_tolerance=0)["status"], "FAIL")
        shifted = trace([30100, 60100, 90100, 120100])
        self.assertEqual(cosim.compare(a, shifted, "cycles", cycle_tolerance=0)["status"], "PASS")
        self.assertEqual(cosim.compare(a, shifted, "cycles", cycle_tolerance=0, equal_cycle_origin=True)["status"], "FAIL")
        for values in ([0, 0, 0, 0], [30000, 90000, 120000, 150000], [4, 3, 2, 1]):
            self.assertEqual(cosim.compare(a, trace(values), "cycles")["status"], "FAIL")

    def test_cycle_interval_budget_not_cumulative_or_stride_scaled(self):
        a = cosim.Trace({f: f * 30000 for f in range(1, 8)}, 1, 7, 7)
        b = cosim.Trace({f: f * 30000 + 100 + f for f in range(1, 8)}, 1, 7, 7)
        self.assertEqual(cosim.compare(a, b, "cycles", cycle_tolerance=1)["status"], "PASS")
        result = cosim.compare(a, b, "cycles", start=1, end=7, stride=2, cycle_tolerance=1)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["first_mismatch"]["from_frame"], 1)
        self.assertEqual(result["first_mismatch"]["frame"], 3)

    def test_known_faults_localize_and_unknown_fault_rejected(self):
        clean = cosim.parse_trace(hashes(), "hash")
        for region, sub in (("ram", "ram"), ("oam", "ppu_mem")):
            rows = hashes()
            rows[1]["sub"][sub] = "f" * 16
            fault = cosim.parse_trace(rows, "hash")
            self.assertEqual(cosim.fault_result(clean, fault, 2, region)["status"], "PASS")
            self.assertEqual(cosim.fault_result(clean, fault, 3, region)["status"], "FAIL")
            self.assertEqual(cosim.fault_result(clean, clean, 2, region)["status"], "FAIL")
            wrong_region = "oam" if region == "ram" else "ram"
            self.assertEqual(cosim.fault_result(clean, fault, 2, wrong_region)["status"], "FAIL")
        with self.assertRaises(cosim.TraceError):
            cosim.fault_result(clean, clean, 2, "unknown")

    def test_fault_accepts_intentional_future_lengths_but_repeat_does_not(self):
        clean = cosim.parse_trace(hashes(), "hash")
        for rows in (hashes(count=3), hashes(count=8), hashes()):
            rows[1]["sub"]["ram"] = "f" * 16
            if len(rows) == 5:
                rows.insert(-1, dict(rows[-2]))
            fault = cosim.parse_trace(rows, "hash")
            result = cosim.fault_result(clean, fault, 2, "ram")
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["prefix_coverage_equal"])
            self.assertTrue(result["post_injection_coverage"]["differs"])
            self.assertEqual(result["checkpoints"], {"start": 1, "end": 2, "stride": 1})
            self.assertEqual(result["first_mismatch"]["frame"], 2)
            self.assertEqual(cosim.compare(clean, fault, "hash")["status"], "FAIL")

    def test_fault_rejects_unequal_prefix_emissions_including_injection_frame(self):
        clean = cosim.parse_trace(hashes(), "hash")
        for extra_frame in (1, 2):
            rows = hashes(count=8)
            rows[1]["sub"]["ram"] = "f" * 16
            rows.insert(extra_frame, dict(rows[extra_frame - 1]))
            fault = cosim.parse_trace(rows, "hash")
            result = cosim.fault_result(clean, fault, 2, "ram")
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(result["prefix_coverage_equal"])
            self.assertEqual(result["first_mismatch"]["kind"], "emission_coverage")

    def test_fault_rejects_prior_transient_divergence_and_missing_prefix(self):
        clean_rows = hashes()
        clean_rows.insert(1, dict(clean_rows[0]))
        clean = cosim.parse_trace(clean_rows, "hash")
        rows = hashes(count=8)
        rows.insert(1, dict(rows[0]))
        rows[0] = {**rows[0], "sub": {**rows[0]["sub"], "cpu": "f" * 16}}
        rows[2]["sub"]["ram"] = "f" * 16
        fault = cosim.parse_trace(rows, "hash")
        result = cosim.fault_result(clean, fault, 2, "ram")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["first_mismatch"]["frame"], 1)
        self.assertEqual(result["first_mismatch"]["emission"], 0)
        for missing_frame in (1, 2):
            fault = cosim.parse_trace(rows, "hash")
            del fault.frames[missing_frame]
            result = cosim.fault_result(clean, fault, 2, "ram")
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["first_mismatch"]["kind"], "coverage")
        fault = cosim.parse_trace(hashes(count=6, start=2), "hash")
        self.assertEqual(cosim.fault_result(clean, fault, 3, "ram")["status"], "FAIL")


class ProcessTests(unittest.TestCase):
    def test_revision_includes_dirty_diff_and_untracked_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "new.c").write_bytes(b"new source")
            diff = b"staged and unstaged diff"
            replies = [b"revision\n", diff, b"new.c\0", b" M tracked.c\n?? new.c\n"]
            with patch.object(cosim.subprocess, "check_output", side_effect=replies) as git:
                revision = cosim.engine_revision(root)
            self.assertTrue(revision["dirty"])
            self.assertEqual(revision["revision"], "revision")
            self.assertEqual(revision["diff_sha256"], cosim.hashlib.sha256(diff).hexdigest())
            self.assertEqual(revision["untracked"], {"new.c": cosim.sha256(root / "new.c")})
            self.assertIn("HEAD", git.call_args_list[1].args[0])
            self.assertIn("--binary", git.call_args_list[1].args[0])

    def test_failure_and_timeout_retain_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            result = cosim.run_process([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"],
                                      temp, dict(os.environ), 10)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["returncode"], 7)
            self.assertIn("out", Path(result["stdout"]).read_text())
            self.assertIn("err", Path(result["stderr"]).read_text())
            result = cosim.run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                      temp, dict(os.environ), 0.1)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["reason"], "subprocess timeout")

    @unittest.skipUnless(os.name == "posix", "POSIX signal return code")
    def test_crash_and_missing_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            result = cosim.run_process([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
                                      temp, dict(os.environ), 10)
            self.assertEqual(result["status"], "FAIL")
            self.assertLess(result["returncode"], 0)
            result = cosim.run_process([str(Path(temp) / "absent")], temp, dict(os.environ), 10)
            self.assertEqual(result["status"], "FAIL")
            self.assertIsNone(result["returncode"])

    def test_engine_isolation_environment_and_smoke_clock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = argparse.Namespace(exe=sys.executable, nesref=sys.executable, core=__file__,
                                      rom=__file__, frames=6, timeout=10)
            runs = []
            def process(command, directory, env, timeout):
                self.assertNotEqual(Path(command[0]), Path(sys.executable))
                self.assertEqual(Path(command[0]).parent, directory)
                self.assertEqual(env["HOME"], str(directory))
                self.assertNotIn("NESREF_FREEZE", env)
                self.assertNotIn("NESRECOMP_COSIM_INJECT", env)
                self.assertNotIn("NESRECOMP_FREEZE", env)
                self.assertNotIn("COSIM_BENIGN", env)
                self.assertEqual(Path(command[1]).parent, directory)
                self.assertEqual(Path(command[1]).read_bytes(), Path(__file__).read_bytes())
                (directory / "smoke.json").write_text('{"frames_run":6,"dispatch_miss_count":0}')
                # Native guest frames may exceed completed host callbacks. No end markers.
                write_rows(directory / "hash.jsonl", hashes(count=8, start=0)[:-1])
                data = deltas(end=7)[:-1]
                for row in data:
                    if row["f"] == 1:
                        row["f"] = 0
                write_rows(directory / "ram.jsonl", data)
                return {"status": "PASS", "returncode": 0}
            dirty_env = {"NESREF_FREEZE": "bad", "NESRECOMP_FREEZE": "bad",
                         "NESRECOMP_COSIM_INJECT": "bad", "COSIM_BENIGN": "0x01"}
            with patch.dict(os.environ, dirty_env), patch.object(cosim, "run_process", side_effect=process):
                paths = cosim.run_engine(args, root, "clean", "recomp", runs)
            trace = cosim.checked_trace(paths["hash"], "hash", args, "recomp")
            self.assertEqual(trace.end, 7)
            ram = cosim.checked_trace(paths["ram"], "ram", args, "recomp")
            self.assertEqual(ram.end, 7)
            self.assertEqual(ram.frames[7][7], 255)
            self.assertEqual(runs[0]["smoke_frames_run"], 6)
            status = [{"type": "frame", "f": f} for f in range(1, 7)]
            status.append({"type": "complete", "f": 6, "requested_frames": 6, "reason": "frame_limit"})
            write_rows(paths["hash"].parent / "status.jsonl", status)
            with self.assertRaisesRegex(cosim.TraceError, "backwards"):
                cosim.checked_trace(paths["hash"], "hash", args, "oracle")

    def test_oracle_sidecar_carry_forward_and_truncation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace = root / "ram.jsonl"
            write_rows(trace, deltas()[:-1])
            path = root / "status.jsonl"
            rows = [{"type": "frame", "f": f} for f in range(1, 5)]
            rows.append({"type": "complete", "f": 4, "requested_frames": 4, "reason": "frame_limit"})
            write_rows(path, rows)
            result = cosim.load_trace(trace, "ram", path, 4)
            self.assertEqual(result.frames[4][7], 255)
            for bad in (rows[:-1], rows[:1] + rows[2:], rows[:-1] + [dict(rows[-1], reason="script_done")]):
                write_rows(path, bad)
                with self.assertRaises(cosim.TraceError):
                    cosim.load_trace(trace, "ram", path, 4)

    def test_empty_truncated_jsonl_and_bool_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            for text in ('{"f":1}', '{"f":', '\n'):
                path.write_text(text)
                with self.assertRaises(cosim.TraceError):
                    cosim.load_rows(path)
            path.write_text("")
            with self.assertRaisesRegex(cosim.TraceError, "empty"):
                cosim.checked_trace(path, "hash", argparse.Namespace(frames=4), "recomp")
            write_rows(path, [{"type": "frame", "f": True},
                              {"type": "complete", "f": 1, "requested_frames": 1, "reason": "frame_limit"}])
            with self.assertRaises(cosim.TraceError):
                cosim.oracle_completion(path, 1)

    def test_native_heartbeat_missing_frame_or_initial_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hash.jsonl"
            args = argparse.Namespace(frames=6)
            write_rows(path, hashes()[:-1])
            with self.assertRaisesRegex(cosim.TraceError, "coverage through 5"):
                cosim.checked_trace(path, "hash", args, "recomp")
            rows = hashes(count=6)[:-1]
            del rows[3]
            write_rows(path, rows)
            with self.assertRaisesRegex(cosim.TraceError, "missing hash frame 4"):
                cosim.checked_trace(path, "hash", args, "recomp")
            write_rows(path, hashes(count=6)[:-1])
            rows = deltas(end=6)[:-1]
            for row in rows:
                if row["f"] == 1:
                    row["f"] = 2
            write_rows(path.parent / "ram.jsonl", rows)
            with self.assertRaisesRegex(cosim.TraceError, "heartbeat start"):
                cosim.checked_trace(path.parent / "ram.jsonl", "ram", args, "recomp")

    def test_early_smoke_completion_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(exe=sys.executable, rom=__file__, frames=6,
                                      timeout=10)
            def process(command, directory, env, timeout):
                (directory / "smoke.json").write_text('{"frames_run":5}')
                return {"status": "PASS", "returncode": 0}
            with patch.object(cosim, "run_process", side_effect=process), self.assertRaisesRegex(cosim.TraceError, "exited early"):
                cosim.run_engine(args, Path(temp), "early", "recomp", [])

    def test_bad_smoke_metrics_rejected(self):
        for metrics in ({"frames_run": 6}, {"frames_run": True, "dispatch_miss_count": 0},
                        {"frames_run": 7, "dispatch_miss_count": 0},
                        {"frames_run": 6, "dispatch_miss_count": 1},
                        {"frames_run": 6, "dispatch_miss_count": False}):
            with self.subTest(metrics=metrics), tempfile.TemporaryDirectory() as temp:
                args = argparse.Namespace(exe=sys.executable, rom=__file__, frames=6, timeout=10)
                def process(command, directory, env, timeout):
                    (directory / "smoke.json").write_text(json.dumps(metrics))
                    return {"status": "PASS", "returncode": 0}
                with patch.object(cosim, "run_process", side_effect=process), self.assertRaises(cosim.TraceError):
                    cosim.run_engine(args, Path(temp), "bad", "recomp", [])

    def test_verdict_never_empty_green(self):
        self.assertEqual(cosim.verdict([]), "UNSUPPORTED")
        self.assertEqual(cosim.verdict([{"status": "PASS"}, {"status": "UNSUPPORTED"}]), "UNSUPPORTED")
        self.assertEqual(cosim.verdict([{"status": "FAIL"}, {"status": "UNSUPPORTED"}]), "FAIL")
        self.assertEqual(cosim.verdict([{"status": "unknown"}]), "FAIL")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.common = ["--exe", sys.executable, "--rom", __file__, "--nesref", sys.executable,
                       "--core", __file__, "--frames", "6", "--fault-frame", "3", "--out", str(self.root)]
        self.inputs = []
        self.revision = {"revision": cosim.MESEN_REVISION, "dirty": False,
                         "diff_sha256": "0" * 64, "untracked": {}}
        for mock in (patch.object(cosim, "RUNS_ROOT", self.root),
                     patch.object(cosim, "engine_revision", return_value=self.revision),
                     patch.object(cosim, "rom_info", return_value={"sha256": cosim.sha256(__file__)})):
            mock.start()
            self.addCleanup(mock.stop)

    def process(self, command, directory, env, timeout):
        self.inputs.append((command, directory, env))
        recomp = "NESRECOMP_COSIM_HASH" in env
        requested = int(command[command.index("--smoke") + 1]) if recomp else int(env["NESREF_FRAMES"])
        count, start = (requested + 2, 0) if recomp else (requested, 1)
        if recomp:
            (directory / "smoke.json").write_text(json.dumps({"frames_run": requested, "dispatch_miss_count": 0}))
            rows = hashes(count=count, start=start)[:-1]
            # Repeated NMI emits must not be collapsed in gate1/gate3.
            rows.insert(1, dict(rows[0]))
            injection = env.get("NESRECOMP_COSIM_INJECT")
            if injection:
                frame, region, _, _ = injection.split(":")
                sub = "ram" if region == "ram" else "ppu_mem"
                next(row for row in rows if row["f"] == int(frame))["sub"][sub] = "f" * 16
            write_rows(directory / "hash.jsonl", rows)
        else:
            status = [{"type": "frame", "f": f} for f in range(1, requested + 1)]
            status.append({"type": "complete", "f": requested, "requested_frames": requested, "reason": "frame_limit"})
            write_rows(directory / "status.jsonl", status)
            write_rows(directory / "cycle.jsonl", [{"f": f, "cyc": f * 30000 + 100} for f in range(1, requested + 1)])
        for kind in ("ram", "ppu"):
            data = deltas(kind, count)[:-1]
            for row in data:
                if row["f"] == 1:
                    row["f"] = start
            if kind == "ppu":
                data.extend({"f": 3, "adr": hex(a), "val": "0xff"} for a in (0x100, 0x200))
            write_rows(directory / (kind + ".jsonl"), data)
        return {"status": "PASS", "returncode": 0}

    def invoke(self, command, extra=(), process=None):
        output = io.StringIO()
        with patch.object(cosim, "run_process", side_effect=process or self.process), contextlib.redirect_stdout(output):
            code = cosim.main([command, *self.common, *extra])
        self.output = output.getvalue()
        report = max(self.root.glob(f"{command}-*/summary.json"), key=lambda p: p.stat().st_mtime_ns)
        return code, json.loads(report.read_text())

    def test_live_gates_and_fixed_cross_domain(self):
        for gate in ("gate1", "gate2", "gate3", "selftest"):
            code, report = self.invoke(gate)
            self.assertEqual(code, 0, report)
        self.assertEqual(report["gates"], {"gate1": "PASS", "gate2": "PASS", "gate3": "PASS"})
        self.assertEqual(len(report["runs"]), 6)
        code, report = self.invoke("compare")
        self.assertEqual(code, 2)
        self.assertEqual([run["requested_frames"] for run in report["runs"]], [6, 6])
        self.assertEqual(report["runs"][0]["coverage"]["hash"]["end"], 7)
        for check in report["checks"][:5]:
            self.assertEqual(check["status"], "PASS", check)
            self.assertEqual(check["checkpoints"], {"start": 1, "end": 5, "stride": 1})
            self.assertEqual(check["compared_frames"], 5)
        for channel in ("cpu", "mapper", "chr"):
            self.assertEqual(next(c for c in report["checks"] if c["channel"] == channel)["status"], "UNSUPPORTED")
        self.assertEqual(report["engine"]["diff_sha256"], "0" * 64)
        for artifact in report["artifacts"].values():
            self.assertEqual(cosim.sha256(artifact["snapshot"]), artifact["sha256"])

    def test_default_frame60_injection_contract(self):
        code, report = self.invoke("selftest", ["--frames", "64", "--fault-frame", "60"])
        self.assertEqual(code, 0, report)
        injections = [env["NESRECOMP_COSIM_INJECT"] for _, _, env in self.inputs if "NESRECOMP_COSIM_INJECT" in env]
        self.assertEqual(injections, ["60:ram:0x300:ff", "60:oam:7:ff"])
        faults = [c for c in report["checks"] if c["channel"].startswith("fault_")]
        self.assertEqual([c["first_mismatch"]["frame"] for c in faults], [60, 60])
        self.assertEqual([c["first_mismatch"]["subsystems"] for c in faults], [["ram"], ["ppu_mem"]])
        for gate in ("gate1", "gate2", "gate3"):
            self.assertIn(f"{gate}: PASS", self.output)
        self.assertIn('gate3/fault_ram: PASS; first mismatch: {"frame": 60', self.output)
        self.assertIn('gate3/fault_oam: PASS; first mismatch: {"frame": 60', self.output)
        self.assertLess(self.output.index("first mismatch"), self.output.index("summary.json"))

    def test_fault_future_coverage_cascade_requires_complete_valid_runs(self):
        for failure in (None, "early", "crash", "timeout", "short_trace", "missing_frame", "partial_row"):
            with self.subTest(failure=failure):
                def process(command, directory, env, timeout):
                    result = self.process(command, directory, env, timeout)
                    if directory.name == "fault-ram":
                        path = directory / "hash.jsonl"
                        rows = cosim.load_rows(path)
                        # A valid longer future, with the same full prefix and injection.
                        rows.extend(hashes(count=4, start=8)[:-1])
                        if failure == "early":
                            (directory / "smoke.json").write_text('{"frames_run":5,"dispatch_miss_count":0}')
                        elif failure in ("crash", "timeout"):
                            result = {"status": "FAIL", "returncode": -9,
                                      "reason": "subprocess timeout" if failure == "timeout" else "nonzero subprocess exit"}
                        elif failure == "short_trace":
                            rows = [row for row in rows if row["f"] <= 3]
                        elif failure == "missing_frame":
                            rows = [row for row in rows if row["f"] != 6]
                        write_rows(path, rows)
                        if failure == "partial_row":
                            with path.open("a") as stream:
                                stream.write('{"f":12')
                    return result
                code, report = self.invoke("selftest", process=process)
                self.assertEqual(report["gates"]["gate1"], "PASS")
                self.assertEqual(report["gates"]["gate2"], "PASS")
                if failure is None:
                    self.assertEqual(code, 0, report)
                    fault = next(c for c in report["checks"] if c["channel"] == "fault_ram")
                    self.assertEqual(fault["first_mismatch"]["frame"], 3)
                    self.assertEqual(fault["post_injection_coverage"]["a_end"], 7)
                    self.assertEqual(fault["post_injection_coverage"]["b_end"], 11)
                    self.assertTrue(fault["post_injection_coverage"]["differs"])
                else:
                    self.assertEqual(code, 1)
                    self.assertEqual(report["gates"]["gate3"], "FAIL")
                    self.assertFalse(any(c["channel"] == "fault_ram" and c["status"] == "PASS" for c in report["checks"]))
                    run = next(r for r in report["runs"] if r["tag"] == "fault-ram")
                    self.assertEqual(run["status"], "FAIL")
                    self.assertIn("gate3/harness: FAIL", self.output)

    def test_repeat_artifacts_frozen_across_concurrent_rebuild(self):
        original = self.root / "native-build"
        original.write_bytes(b"build A")
        def process(command, directory, env, timeout):
            self.assertEqual(Path(command[0]).read_bytes(), b"build A")
            original.write_bytes(b"build B")
            return self.process(command, directory, env, timeout)
        code, report = self.invoke("gate1", ["--exe", str(original)], process)
        self.assertEqual(code, 0, report)
        self.assertEqual(original.read_bytes(), b"build B")
        self.assertEqual(len({r["executable_sha256"] for r in report["runs"]}), 1)
        self.assertNotEqual(report["artifacts"]["exe"]["sha256"], cosim.sha256(original))

    def test_full_row_repeat_does_not_hide_duplicate_transient(self):
        def process(command, directory, env, timeout):
            result = self.process(command, directory, env, timeout)
            if directory.name == "native-b":
                rows = cosim.load_rows(directory / "hash.jsonl")
                rows[0]["sub"]["cpu"] = "f" * 16
                write_rows(directory / "hash.jsonl", rows)
            return result
        code, report = self.invoke("gate1", process=process)
        self.assertEqual(code, 1)
        self.assertEqual(report["checks"][0]["first_mismatch"]["emission"], 0)
        self.assertIn("gate1: FAIL", self.output)
        self.assertIn('gate1/hash: FAIL; first mismatch: {"frame": 0', self.output)

    def test_repeat_floor_required_and_oracle_still_runs_after_native_failure(self):
        def process(command, directory, env, timeout):
            if "NESRECOMP_COSIM_HASH" in env:
                return {"status": "FAIL", "returncode": 7, "reason": "fixture failure"}
            return self.process(command, directory, env, timeout)
        code, report = self.invoke("selftest", process=process)
        self.assertEqual(code, 1)
        self.assertEqual(report["gates"], {"gate1": "FAIL", "gate2": "PASS", "gate3": "FAIL"})
        self.assertFalse(any("NESRECOMP_COSIM_INJECT" in env for _, _, env in self.inputs))

    def test_compare_missing_checkpoint_fails_not_auto_trimmed(self):
        code, report = self.invoke("compare", ["--end", "8"])
        self.assertEqual(code, 1)
        self.assertEqual(report["checks"][0]["first_mismatch"]["frame"], 7)

    def test_compare_controls_explicitly_reported(self):
        code, report = self.invoke("compare", ["--mask-stack", "--start", "1", "--end", "5", "--stride", "2"])
        self.assertEqual(code, 2)
        self.assertEqual(report["checks"][0]["masked_bytes_per_frame"], 256)
        self.assertEqual(report["checks"][0]["compared_frames"], 3)

    def test_oracle_status_missing_frame_fails_even_with_complete_traces(self):
        def process(command, directory, env, timeout):
            result = self.process(command, directory, env, timeout)
            rows = cosim.load_rows(directory / "status.jsonl")
            del rows[2]
            write_rows(directory / "status.jsonl", rows)
            return result
        code, report = self.invoke("gate2", process=process)
        self.assertEqual(code, 1)
        self.assertIn("coverage", report["checks"][0]["reason"])

    def test_oracle_repeat_does_not_collapse_duplicate_cycle_rows(self):
        def process(command, directory, env, timeout):
            result = self.process(command, directory, env, timeout)
            if directory.name == "oracle-b":
                rows = cosim.load_rows(directory / "cycle.jsonl")
                rows.insert(0, {"f": 1, "cyc": 1})
                write_rows(directory / "cycle.jsonl", rows)
            return result
        code, report = self.invoke("gate2", process=process)
        self.assertEqual(code, 1)
        self.assertEqual(next(c for c in report["checks"] if c["channel"] == "oracle_cycle_emissions")["status"], "FAIL")

    def test_invalid_rom_fails_before_any_engine_launch(self):
        with patch.object(cosim, "rom_info", cosim._project.rom_info):
            code, report = self.invoke("compare")
        self.assertEqual(code, 1)
        self.assertIn("verified Guardian Legend", report["checks"][0]["reason"])
        self.assertEqual(report["runs"], [])
        self.assertIn("harness/harness: FAIL; ROM does not match", self.output)

    def test_output_must_remain_under_retained_root(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cosim.main(["gate1", "--out", str(self.root.parent)])

    @unittest.skipUnless(os.name == "posix", "executable script fixture")
    def test_real_zero_exit_without_smoke_is_failure_and_saves_untouched(self):
        executable = self.root / "fake-native"
        executable.write_text("#!/usr/bin/env python3\nprint('zero exit is not completion')\n")
        executable.chmod(0o755)
        saves = self.root / "saves"
        saves.mkdir()
        sentinel = saves / "user.sav"
        sentinel.write_bytes(b"do not delete")
        with contextlib.redirect_stdout(io.StringIO()):
            code = cosim.main(["gate1", *self.common, "--exe", str(executable)])
        self.assertEqual(code, 1)
        report_path = next(self.root.glob("gate1-*/summary.json"))
        report = json.loads(report_path.read_text())
        self.assertIn("completion", report["checks"][0]["reason"])
        self.assertEqual(report["runs"][0]["returncode"], 0)
        self.assertIn("zero exit", Path(report["runs"][0]["stdout"]).read_text())
        self.assertEqual(sentinel.read_bytes(), b"do not delete")

    @unittest.skipUnless(os.name == "posix", "executable script fixture")
    def test_real_zero_exit_with_early_smoke_is_not_completion(self):
        executable = self.root / "fake-native"
        executable.write_text(
            "#!/usr/bin/env python3\nimport json, sys\n"
            "path = sys.argv[sys.argv.index('--smoke-output') + 1]\n"
            "with open(path, 'w') as f: json.dump({'frames_run': 0, 'dispatch_miss_count': 0}, f)\n")
        executable.chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()):
            code = cosim.main(["gate1", *self.common, "--exe", str(executable)])
        self.assertEqual(code, 1)
        report = json.loads(next(self.root.glob("gate1-*/summary.json")).read_text())
        self.assertEqual(report["runs"][0]["returncode"], 0)
        self.assertEqual(report["runs"][0]["smoke_frames_run"], 0)
        self.assertIn("exited early", report["checks"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
