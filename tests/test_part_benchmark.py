"""The offline benchmark scaffold: deterministic datasets, expected answers kept apart, honest scoring, plan rows without Claude."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("part_benchmark", Path(__file__).resolve().parent.parent / "scripts/part_benchmark.py")
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def honest(expected):
    return "\n".join("%s: %s" % (f["id"], ", ".join(f["values"])) for f in expected["facts"])


class DatasetTests(unittest.TestCase):
    def test_every_dataset_is_deterministic_with_the_true_values_on_the_planted_lines(self):
        for name in bench.DATASETS:
            with self.subTest(name):
                raw, expected = bench.build(name)
                self.assertEqual((raw, expected), bench.build(name))
                lines = raw.decode("utf-8").split("\n")
                for fact in expected["facts"]:
                    line = lines[fact["line"] - 1]
                    self.assertIn(fact["id"], line)
                    for value in fact["values"]:
                        self.assertIn(value, lines[fact["line"] - 1] if name != "kod-zaleznosci" else "\n".join(lines))
                self.assertLessEqual(len(raw), 5 * 1024 * 1024)

    def test_expected_answers_are_written_apart_from_the_material_and_the_questions_are_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            bench.main(["write", str(out)])
            self.assertEqual(sorted(p.name for p in (out / "data").iterdir()), sorted(n + ".txt" for n in bench.DATASETS))
            self.assertEqual(sorted(p.name for p in (out / "expected").iterdir()), sorted(n + ".json" for n in bench.DATASETS))
            for name in bench.DATASETS:
                data = (out / "data" / (name + ".txt")).read_text()
                expected = json.loads((out / "expected" / (name + ".json")).read_text())
                self.assertNotIn('"values"', data)                               # nothing of the answer key inside the data
                self.assertNotIn(expected["question"], data)
            self.assertEqual(json.loads((out / "questions.json").read_text()), bench.QUESTIONS)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        _, self.expected = bench.build("log-zroznicowany")

    def test_a_true_answer_scores_fully(self):
        result = bench.score(honest(self.expected), self.expected)
        self.assertEqual((result["identifier_recall"], result["value_correct"], result["fabricated"], result["wrong"]), (1.0, 1.0, [], []))

    def test_listing_identifiers_as_not_found_is_full_identifier_recall_but_zero_correct_values(self):
        ids = ", ".join(f["id"] for f in self.expected["facts"])
        result = bench.score("Nie znaleziono żadnego z identyfikatorów: %s." % ids, self.expected)
        self.assertEqual((result["identifier_recall"], result["value_correct"]), (1.0, 0.0))
        self.assertEqual(len(result["wrong"]), len(self.expected["facts"]))

    def test_a_wrong_value_is_wrong_and_an_invented_identifier_is_fabricated(self):
        facts = self.expected["facts"]
        answer = honest(self.expected).replace(facts[0]["values"][1], "00") + "\nINC-99999: severity=high retries=11"
        result = bench.score(answer, self.expected)
        self.assertIn(facts[0]["id"], result["wrong"])
        self.assertEqual(result["fabricated"], ["INC-99999"])
        self.assertLess(result["value_correct"], 1.0)

    def test_a_missing_fact_is_listed_and_a_value_of_another_fact_does_not_count(self):
        facts = self.expected["facts"]
        answer = "\n".join("%s: %s" % (f["id"], ", ".join(facts[1]["values"] if f is facts[0] else f["values"])) for f in facts[1:] )
        answer += "\n%s: %s" % (facts[0]["id"], ", ".join(["zzz"]))
        result = bench.score(answer, self.expected)
        self.assertEqual(result["missing"], [])
        self.assertIn(facts[0]["id"], result["wrong"])

    def test_a_dependency_counts_as_linked_only_when_both_markers_and_the_symbol_are_named_together(self):
        _, expected = bench.build("kod-zaleznosci")
        good = "LIMIT_ZEROED_BY_MIGRATION: wyzerowany w DEP-CAUSE-7731, sprawdzany w DEP-EFFECT-7731."
        apart = "DEP-CAUSE-7731 ustawia LIMIT_ZEROED_BY_MIGRATION.\nDEP-EFFECT-7731 to nieudany assert."
        self.assertEqual(bench.score(good, expected)["linked"], 1)
        self.assertEqual(bench.score(apart, expected)["linked"], 0)


class PlanTests(unittest.TestCase):
    def test_the_512_profile_needs_fewer_requests_and_every_packet_stays_inside_the_transport_ceiling(self):
        raw, expected = bench.build("log-zroznicowany")
        small = bench.plan_row(raw, 240 * 1024, expected["question"])
        large = bench.plan_row(raw, 512 * 1024, expected["question"])
        self.assertLess(large["requests"], small["requests"])
        self.assertLessEqual(large["largest_packet_kib"] * 1024, bench.ac.MAX_PACKET_LARGE)
        self.assertEqual((small["cap"], large["cap"]), (small["requests"] + 1, large["requests"] + 1))
        self.assertGreater(large["packet_bytes_max"], large["data_bytes"])        # packets carry more than the dynamic data alone

    def test_the_declared_run_set_fits_the_request_limit_and_the_packet_bound_covers_the_earlier_measurement(self):
        rows = bench.run_rows()
        self.assertLessEqual(sum(r["cap"] for r in rows.values()), bench.REQUEST_LIMIT)
        self.assertEqual(sum(r["cap"] for r in rows.values()), 43)
        # offline measurement of correct packets with the real questions and maximal quote-only notes (14 103 062 B)
        self.assertGreaterEqual(sum(r["packet_bytes_max"] for r in rows.values()), 14103062)

    def test_the_bound_covers_every_allowed_serialization_of_a_note_and_every_step_number(self):
        from ai_desktop import analysis_contract as ac, material_loop as loop, material_step as step, material_whole as whole, materials
        raw, expected = bench.build("log-zroznicowany")
        row = bench.plan_row(raw, 240 * 1024, expected["question"])
        mat = materials.import_bytes(raw, "file")
        plan = whole.WholePlan(mat, part_bytes=240 * 1024)
        # the largest expansion of an allowed note byte is 2x (quote, backslash, newline, tab); everything else is <= 1x
        characters = [chr(c) for c in range(0x20, 0x250)] + ["\n", "\t", "ż", "€", "\U0001F600", "\u2028"]
        worst = 0
        for ch in characters:
            if ch in ("\x7f",):
                continue
            note = (ch * (step.NOTE_BYTES // len(ch.encode())))
            plan.notes = {p.index: note for p in plan.parts}
            try:
                size = len(ac.application_input(None, expected["question"], material=plan.reduce_input(8, step.TOTAL_BYTES)).encode())
            except (ValueError, ac.AnalysisDenied):
                continue                                                          # a character the schema itself refuses
            worst = max(worst, size)
            self.assertLessEqual(size, row["reduce_bytes_max"], repr(ch))
        plan.notes = {}
        self.assertEqual(worst, row["reduce_bytes_max"])                         # the quote-only note IS the maximum
        for number in range(1, 9):                                               # step numbers and byte counters of any width
            for left in (0, 9, 99999, step.TOTAL_BYTES):
                for p in plan.parts:
                    size = len(ac.application_input(None, expected["question"], material=plan.map_input(p.index, number, left)).encode())
                    self.assertLessEqual(size, row["packet_bytes_max"])
        first = len(ac.application_input(None, expected["question"], material=loop.Loop(mat).request_input(1, 0)).encode())
        self.assertLessEqual(first, row["initial_bytes"])

    def test_the_three_quantities_are_named_apart_and_the_client_traffic_is_not_claimed(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bench.main(["plan", "--limit", "45"])
        text = out.getvalue()
        for word in ("data_bytes", "packet_bytes_max", "is NOT measured"):
            self.assertIn(word, text)
        self.assertNotIn("on the wire", text)
        self.assertNotIn("wire_bytes", text)

    def test_the_plan_fails_when_the_cap_sum_exceeds_the_limit(self):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench.main(["plan", "--limit", "42"]), 1)
            self.assertEqual(bench.main(["plan", "--limit", "43"]), 0)


def write_run(directory, label, *, code=0, phases=None, models=("claude-x", 200000), result="complete", covered=None, synthesis="answer",
              parts_order=None, status=True):
    """What the app leaves behind for one run: status file, --meta-log hand-offs and the controller's result line."""
    directory = Path(directory)
    row = bench.run_rows()[label]
    phases = phases if phases is not None else ["loop"] + ["map"] * row["parts"] + ["reduce"]
    entry = {"model": models[0], "window": models[1], "input": 1, "output": 1} if models else None
    maps = iter(parts_order or range(1, row["parts"] + 1))
    records = [{"n": n + 1, "phase": ph, "part": next(maps, None) if ph == "map" else None, "models": [entry]} for n, ph in enumerate(phases)]
    lines = [json.dumps(r) for r in records]
    if result is not None:
        lines.append(json.dumps({"result": {"outcome": result, "covered": row["parts"] if covered is None else covered,
                                            "total": row["parts"], "synthesis": synthesis}}))
    (directory / ("meta-%s.jsonl" % label)).write_text("\n".join(lines) + "\n")
    if status:
        (directory / ("status-%s.txt" % label)).write_text("%d\n" % code)


class ExecuteTests(unittest.TestCase):
    def run_execute(self, tmp, code=0, binary="/usr/bin/ai-desktop", **run):
        from unittest.mock import patch, MagicMock
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if run.get("interrupt"):
                raise KeyboardInterrupt()
            if run.get("write", True):
                write_run(tmp, "log-512", status=False, **{k: v for k, v in run.items() if k not in ("interrupt", "write")})
            result = MagicMock()
            result.returncode = code
            return result
        with patch("shutil.which", return_value=binary), patch("subprocess.run", side_effect=fake_run), \
                patch("builtins.input", side_effect=AssertionError("nothing may wait for or fake an answer")), patch("builtins.print"):
            status = bench.execute("log-512", tmp)
        return status, calls

    def test_a_complete_correct_run_returns_zero_and_is_one_interactive_capped_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, calls = self.run_execute(tmp)
            command, kwargs = calls[0]
            self.assertEqual((status, len(calls)), (0, 1))                       # no clipboard step, no second process
            cap = bench.run_rows()["log-512"]["cap"]
            question = bench.QUESTIONS["log-zroznicowany"]
            self.assertEqual(command[:9], ["/usr/bin/ai-desktop", "material", "--v", "--only-whole", "--max-requests", str(cap),
                                           "--first-question", question, "--meta-log"])
            self.assertEqual(command[9], str(Path(tmp) / "meta-log-512.jsonl"))
            self.assertEqual(command[10:], ["--answer-file", str(Path(tmp) / "answer-log-512.txt"),
                                            str(Path(tmp) / "data" / "log-zroznicowany.txt")])
            self.assertEqual(kwargs, {})                                          # inherits the terminal: no stdin/pipes/shell
            self.assertEqual((Path(tmp) / "status-log-512.txt").read_text(), "0\n")

    def test_a_nonzero_process_code_stops_with_that_code_and_keeps_the_evidence(self):
        for code in (2, 130):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                status, _ = self.run_execute(tmp, code=code)
                self.assertEqual(status, code)                                    # propagated to the runner as non-zero
                self.assertEqual((Path(tmp) / "status-log-512.txt").read_text(), "%d\n" % code)
                self.assertTrue((Path(tmp) / "meta-log-512.jsonl").exists())      # evidence stays

    def test_an_interrupt_is_recorded_as_130_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, _ = self.run_execute(tmp, interrupt=True, write=False)
            self.assertEqual(status, 130)
            self.assertEqual((Path(tmp) / "status-log-512.txt").read_text(), "130\n")

    def test_exit_code_zero_is_not_proof_of_completion(self):
        row = bench.run_rows()["log-512"]
        cases = {
            "no results at all": dict(write=False),
            "declined or quit after the first request": dict(phases=["loop"], result="not-whole"),
            "analysis stopped halfway": dict(phases=["loop"] + ["map"] * 3, result="partial", covered=3, synthesis="none"),
            "no controller result line": dict(result=None),
            "synthesis never accepted": dict(result="partial", synthesis="none"),
            "maps received but not accepted as notes": dict(result="partial", covered=row["parts"] - 2, synthesis="none"),
            "parts out of order": dict(parts_order=[2, 1] + list(range(3, row["parts"] + 1))),
            "phases reordered": dict(phases=["loop", "reduce"] + ["map"] * row["parts"]),
        }
        for name, run in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                status, _ = self.run_execute(tmp, code=0, **run)
                self.assertNotEqual(status, 0, name)

    def test_a_missing_installation_stops_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, calls = self.run_execute(tmp, binary=None)
            self.assertEqual((status, calls), (2, []))

    def test_missing_model_metadata_does_not_fail_the_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, _ = self.run_execute(tmp, models=None)
            self.assertEqual(status, 0)


class ConfigTests(unittest.TestCase):
    def test_check_config_is_read_only_and_stops_on_a_mismatch(self):
        from unittest.mock import patch, MagicMock
        policy = MagicMock()
        policy.part_size.return_value = 240 * 1024
        with patch("ai_desktop.permissions.load_policy", return_value=policy) as load:
            self.assertEqual(bench.check_config(240), 0)
            self.assertEqual(bench.check_config(512), 3)
            load.assert_called_with(optional=True)                               # only reads; nothing is written or switched
        with patch("ai_desktop.permissions.load_policy", return_value=None):
            self.assertEqual(bench.check_config(240), 3)


class ReportTests(unittest.TestCase):
    def write(self, directory, label, models, complete=True, notes=True, truthful=True, **run):
        name, _ = bench.RUNS[label]
        row = bench.run_rows()[label]
        if complete:
            write_run(directory, label, models=models, **run)
        else:
            write_run(directory, label, models=models, phases=["loop"] + ["map"] * row["parts"], result="partial", synthesis="none", **run)
        _, expected = bench.build(name)
        text = "\n".join("%s: %s" % (f["id"], ", ".join(f["values"])) for f in expected["facts"])
        (Path(directory) / ("answer-%s.txt" % label)).unlink(missing_ok=True)
        if complete:
            (Path(directory) / ("answer-%s.txt" % label)).write_text(text if truthful else "nic")
        if notes:
            (Path(directory) / ("answer-%s.txt.notes" % label)).write_text(text)

    def run_report(self, tmp):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bench.main(["report", str(tmp)])
        return out.getvalue(), code

    def full(self, tmp):
        for label in bench.RUNS:
            self.write(Path(tmp), label, ("claude-x", 200000))

    def test_four_complete_runs_with_one_model_and_window_pass_and_are_comparable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.full(tmp)
            text, code = self.run_report(tmp)
        self.assertEqual(code, 0)
        self.assertIn("TRANSPORT: OK in all four runs", text)
        self.assertIn("QUALITY COMPARISON: comparable", text)
        self.assertIn("not proof of a general advantage of 512 KiB", text)

    def test_the_report_fails_when_there_are_no_meta_logs_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            text, code = self.run_report(tmp)
        self.assertEqual(code, 1)
        self.assertIn("TRANSPORT: FAILED", text)
        self.assertIn("meta-log missing", text)

    def test_a_process_code_of_2_fails_the_report_even_when_the_phases_look_complete(self):
        for bad in (2, 130):
            with self.subTest(bad), tempfile.TemporaryDirectory() as tmp:
                self.full(tmp)
                self.write(Path(tmp), "json-240", ("claude-x", 200000), code=bad)
                text, code = self.run_report(tmp)
                self.assertEqual(code, 1)
                self.assertIn("json-240: process exit code %d" % bad, text)
                self.assertIn("INCOMPLETE/MISMATCH", text)

    def test_received_replies_are_not_accepted_notes_the_controller_result_decides(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.full(tmp)
            row = bench.run_rows()["log-240"]
            self.write(Path(tmp), "log-240", ("claude-x", 200000), result="partial", covered=row["parts"] - 1, synthesis="none")
            text, code = self.run_report(tmp)
        self.assertEqual(code, 1)
        self.assertIn("controller: partial", text)

    def test_a_missing_synthesis_fails_the_transport_and_the_notes_are_still_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.full(tmp)
            self.write(Path(tmp), "log-512", ("claude-x", 200000), complete=False)
            text, code = self.run_report(tmp)
        self.assertEqual(code, 1)
        self.assertIn("log-512: final answer missing", text)
        line = [l for l in text.splitlines() if l.startswith("log-512")][0]
        self.assertIn("1.00/1.00/0/0", line)                                     # the notes carried every fact

    def test_missing_metadata_or_a_changed_model_only_limits_the_quality_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.full(tmp)
            self.write(Path(tmp), "json-512", ("claude-y", 200000))
            self.write(Path(tmp), "log-240", None)
            text, code = self.run_report(tmp)
        self.assertEqual(code, 0)                                                 # complete transport, no FAIL
        self.assertIn("QUALITY COMPARISON: LIMITED", text)
        self.assertIn("model or window changed", text)
        self.assertIn("metadata missing", text)
        self.assertIn("TRANSPORT: OK", text)

    def test_the_comparison_ignores_token_counters_and_looks_only_at_model_and_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.full(tmp)
            path = Path(tmp) / "meta-log-240.jsonl"
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            for n, record in enumerate(lines[:-1]):
                record["models"][0]["input"] = 1000 + n                            # counters differ per request
            path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
            text, code = self.run_report(tmp)
            self.assertEqual(code, 0)
            self.assertIn("QUALITY COMPARISON: comparable", text)

    def test_a_run_where_the_model_did_not_choose_the_whole_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.full(tmp)
            write_run(tmp, "json-512", phases=["loop"], result="not-whole")
            text, code = self.run_report(tmp)
        self.assertEqual(code, 1)
        self.assertIn("json-512: ", text)


if __name__ == "__main__":
    unittest.main()
