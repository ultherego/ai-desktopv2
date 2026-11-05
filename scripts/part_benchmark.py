#!/usr/bin/env python3
"""Offline benchmark scaffold for part profiles (default 240 KiB vs optional 512 KiB).

`plan`   prints, without Claude, how each profile splits the SAME datasets: parts, requests, stages, dynamic data bytes,
         the upper bound of the serialized application packets and the `--max-requests` cap of each native run.
         Three different quantities are never mixed: dynamic data (text/notes/history), serialized application
         packets (what the app hands to the client) and the client's real network traffic (NOT measured here).
`write`  stores the datasets and the (identical) questions in DIR/data and the expected answers in DIR/expected: the
         expected answers are never inside the directory the model may read from.
`check-config EXPECTED_KIB`  read-only: the part_size the app would load from the owner's permissions.toml.
`cap LABEL` / `run LABEL`    the planned cap / dataset / part size of one native run.
`report DIR`                 per-run transport check, model identity/window comparison and scores from DIR.
`score`  grades one answer text against the expected answers:
           identifier recall  - planted identifiers that appear in the answer (presence only),
           value correct      - identifier together with its true value in the same sentence/line,
           wrong              - identifier given with a missing or different value,
           fabricated         - identifiers of the dataset's pattern that are NOT in the file,
           linked             - cause/effect pairs named together with their connecting symbol.
Coverage 100% is the controller's accounting, not completeness of findings; an answer that lists identifiers without
true values scores 100% identifier recall and 0% value correct.
"""
import argparse
import functools
import json
import os
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_desktop import analysis_contract as ac           # noqa: E402
from ai_desktop import material_loop as loop             # noqa: E402
from ai_desktop import materials as m                    # noqa: E402
from ai_desktop import material_step as step             # noqa: E402
from ai_desktop import material_whole as whole           # noqa: E402

KIB = 1024
PROFILES = {"240KiB": 240 * KIB, "512KiB": 512 * KIB}
SEED = 20261101


def _slots(rng, size, count):
    return sorted(rng.sample(range(size // 20, size - size // 20), count))


def _ids(rng, count, fmt, space):
    return [fmt % n for n in rng.sample(range(space), count)]


def repetitive_log(rng, size=60000):
    lines = ["2026-11-01 12:%02d:%02d INFO worker heartbeat ok queue=%d" % (i // 60 % 60, i % 60, i % 7) for i in range(size)]
    facts = []
    for slot, ident in zip(_slots(rng, size, 3), _ids(rng, 3, "ERR-%06X", 1 << 24)):
        latency = str(rng.randrange(1000, 9999))
        lines[slot] = "2026-11-01 12:00:00 ERROR code %s disk write failed latency_ms=%s" % (ident, latency)
        facts.append({"id": ident, "values": [latency], "line": slot + 1})
    return lines, facts, [], r"ERR-[0-9A-F]{6}"


def varied_events_log(rng, size=80000):
    kinds = ["login", "logout", "upload", "download", "retry", "cache-miss", "timeout", "rotate", "gc", "config-reload"]
    lines = ["evt=%s user=u%03d id=%06d ms=%d" % (rng.choice(kinds), rng.randrange(500), i, rng.randrange(900)) for i in range(size)]
    facts = []
    for slot, ident in zip(_slots(rng, size, 12), _ids(rng, 12, "INC-%05d", 100000)):
        severity, retries = rng.choice(["low", "high", "critical"]), str(rng.randrange(10, 99))
        lines[slot] = "evt=incident %s severity=%s retries=%s" % (ident, severity, retries)
        facts.append({"id": ident, "values": [severity, retries], "line": slot + 1})
    return lines, facts, [], r"INC-\d{5}"


def dense_spec(rng, size=60000):
    lines = ["REQ-%05d The system SHALL handle case %d of feature %d." % (i, rng.randrange(100), rng.randrange(40)) for i in range(size)]
    facts = []
    for slot, ident in zip(_slots(rng, size, 30), _ids(rng, 30, "EXC-%04d", 10000)):
        limit = str(rng.randrange(1000, 9999))
        lines[slot] = "REQ-%05d EXCEPTION %s: the system MUST NOT apply the previous rule when limit %s is exceeded." % (slot, ident, limit)
        facts.append({"id": ident, "values": [limit], "line": slot + 1})
    return lines, facts, [], r"EXC-\d{4}"


def code_with_dependencies(rng, size=240000):
    lines = []
    for i in range(size // 4):
        lines += ["def f_%05d(x):" % i, "    y = x + %d" % rng.randrange(100), "    return y", ""]
    cause, effect = len(lines) // 8, len(lines) - len(lines) // 8
    lines[cause] = "LIMIT_ZEROED_BY_MIGRATION = 0  # DEP-CAUSE-7731"
    lines[effect] = "assert LIMIT_ZEROED_BY_MIGRATION != 0  # DEP-EFFECT-7731 fails here"
    facts = [{"id": "DEP-CAUSE-7731", "values": ["LIMIT_ZEROED_BY_MIGRATION"], "line": cause + 1},
             {"id": "DEP-EFFECT-7731", "values": ["LIMIT_ZEROED_BY_MIGRATION"], "line": effect + 1}]
    relations = [{"a": "DEP-CAUSE-7731", "b": "DEP-EFFECT-7731", "via": "LIMIT_ZEROED_BY_MIGRATION"}]
    return lines, facts, relations, r"DEP-(?:CAUSE|EFFECT)-\d{4}"


def polish_text(rng, size=40000):
    # Fixture data: Polish words kept as escapes, so the multibyte bytes of this dataset stay the same.
    words = ["\u017c\u00f3\u0142\u0107", "g\u0119\u015bl\u0105", "ja\u017a\u0144", "\u0142\u0105ka", "\u015bwit",
             "\u017ad\u017ab\u0142o", "\u0107ma", "ksi\u0119garnia", "w\u0105\u017c", "chrz\u0105szcz"]
    lines = [" ".join(rng.choice(words) for _ in range(12)) + "." for _ in range(size)]
    facts = []
    for slot, ident in zip(_slots(rng, size, 8), _ids(rng, 8, "PL-%04d", 10000)):
        code = str(rng.randrange(1000, 9999))
        lines[slot] = "Uwaga %s: za\u017c\u00f3\u0142\u0107 g\u0119\u015bl\u0105 ja\u017a\u0144 \u2014 kod %s, termin wa\u017cno\u015bci up\u0142ywa w pi\u0105tek." % (ident, code)
        facts.append({"id": ident, "values": [code], "line": slot + 1})
    return lines, facts, [], r"PL-\d{4}"


def json_with_escapes(rng, size=40000):
    lines = [json.dumps({"i": i, "q": 'say "hi" \\ there', "p": "C:\\dir\\file%d" % rng.randrange(99)}) for i in range(size)]
    facts = []
    for slot, ident in zip(_slots(rng, size, 8), _ids(rng, 8, "JS-%04d", 10000)):
        count = str(rng.randrange(1000, 9999))
        lines[slot] = json.dumps({"i": slot, "flag": ident, "count": int(count), "q": '"""\\\\'})
        facts.append({"id": ident, "values": [count], "line": slot + 1})
    return lines, facts, [], r"JS-\d{4}"


DATASETS = {"powtarzalny-log": repetitive_log, "log-zroznicowany": varied_events_log, "specyfikacja": dense_spec,
            "kod-zaleznosci": code_with_dependencies, "polski-utf8": polish_text, "json-escaping": json_with_escapes}
QUESTIONS = {
    "powtarzalny-log": "List every line with the ERROR level: the ERR-… code and the latency_ms value.",
    "log-zroznicowany": "List all incident events: the INC-… identifier, severity and retries.",
    "specyfikacja": "List every EXCEPTION: the EXC-… identifier and the exceeded limit.",
    "kod-zaleznosci": "Which symbol causes the failed assert, and where was it zeroed? Give both DEP-… markers.",
    "polski-utf8": "List every 'Uwaga PL-…' note with its code.",
    "json-escaping": "List every record with a flag field: the JS-… identifier and the count value.",
}


def build(name, seed=SEED):
    """(material bytes, expected) - the expected answers are separate from what is sent to the model."""
    lines, facts, relations, pattern = DATASETS[name](random.Random("%s:%d" % (name, seed)))
    expected = {"dataset": name, "question": QUESTIONS[name], "facts": facts, "relations": relations, "pattern": pattern}
    return ("\n".join(lines) + "\n").encode("utf-8"), expected


def _units(answer):
    return [u for u in re.split(r"\n+|(?<=[.!?;])\s+", answer) if u.strip()]


def score(answer, expected):
    units = _units(answer)
    ids = {f["id"] for f in expected["facts"]}
    present, correct, wrong = [], [], []
    for fact in expected["facts"]:
        carrying = [u for u in units if fact["id"] in u]
        if not carrying:
            continue
        present.append(fact["id"])
        if any(all(re.search(r"(?<![\w])%s(?![\w])" % re.escape(v), u) for v in fact["values"]) for u in carrying):
            correct.append(fact["id"])
        else:
            wrong.append(fact["id"])
    fabricated = sorted(set(re.findall(expected["pattern"], answer)) - ids)
    linked = [r["via"] for r in expected["relations"]
              if any(r["a"] in u and r["b"] in u and r["via"] in u for u in units)]
    total = len(expected["facts"])
    return {"facts": total, "identifier_recall": round(len(present) / total, 3), "value_correct": round(len(correct) / total, 3),
            "wrong": sorted(wrong), "missing": sorted(ids - set(present)), "fabricated": fabricated,
            "relations": len(expected["relations"]), "linked": len(linked)}


RUNS = {  # label -> (dataset, part size KiB); series 240 first, then 512: one config change in between
    "log-240": ("log-zroznicowany", 240), "json-240": ("json-escaping", 240),
    "log-512": ("log-zroznicowany", 512), "json-512": ("json-escaping", 512)}
REQUEST_LIMIT = 45
WORST_NOTE = '"' * step.NOTE_BYTES          # JSON doubles " \\ newline and tab: 2x is the largest expansion of an allowed note


def plan_row(raw, part_bytes, question="Question?"):
    """Dynamic data and an UPPER BOUND of the serialized application packets for a run with this very question.
    Every request carries the real question; the synthesis is measured with maximal notes made of the worst escape
    character (whole note budget of `"`), the first request with the widest step/bytes_left numbers."""
    mat = m.import_bytes(raw, "file")
    try:
        plan = whole.WholePlan(mat, part_bytes=part_bytes)
    except whole.WholeError:
        return {"refused": True}
    requests, data = plan.task_budget()
    maps = [len(ac.application_input(None, question, material=plan.map_input(p.index, 8, step.TOTAL_BYTES)).encode())
            for p in plan.parts]
    plan.notes = {p.index: WORST_NOTE for p in plan.parts}
    reduce = len(ac.application_input(None, question, material=plan.reduce_input(8, step.TOTAL_BYTES)).encode())
    plan.notes = {}
    first = len(ac.application_input(None, question, material=loop.Loop(mat).request_input(8, step.TOTAL_BYTES)).encode())
    return {"size": mat.size, "parts": len(plan.parts), "requests": requests, "stages": len(plan.stages()),
            "largest_part_kib": max(p.size for p in plan.parts) // KIB, "largest_packet_kib": max(maps) // KIB,
            "data_bytes": data, "packet_bytes_max": sum(maps) + reduce + first, "initial_bytes": first,
            "reduce_bytes_max": reduce, "cap": requests + 1}


@functools.lru_cache(maxsize=1)
def run_rows():
    rows = {}
    for label, (name, kib) in RUNS.items():
        raw, expected = build(name)
        rows[label] = plan_row(raw, kib * KIB, expected["question"])
    return rows


def split_log(path):
    """(hand-off records, controller result records) of one --meta-log."""
    lines = _records(path)
    return [r for r in lines if "result" not in r], [r["result"] for r in lines if "result" in r]


def check_run(label, directory):
    """Why a run is NOT a complete, correct transport acceptance (empty list = complete). Exit status 0 of the interactive
    CLI proves nothing (the user may decline or quit); the order of the phases, the accepted parts and the controller's own
    verdict decide. A received map reply is not an accepted note: the controller result says how many were accepted."""
    out, row, problems = Path(directory), run_rows()[label], []
    status, log = out / ("status-%s.txt" % label), out / ("meta-%s.jsonl" % label)
    if not status.exists():
        problems.append("process exit code missing")
    elif status.read_text().strip() != "0":
        problems.append("process exit code %s" % status.read_text().strip())
    if not log.exists():
        problems.append("meta-log missing")
        return problems
    handoffs, results = split_log(log)
    phases = [h["phase"] for h in handoffs]
    expected = ["loop"] + ["map"] * row["parts"] + ["reduce"]
    if phases != expected:
        problems.append("phases do not match the plan (%d hand-offs, expected %d: loop, %d maps, synthesis)" % (len(handoffs), len(expected), row["parts"]))
    elif [h["part"] for h in handoffs if h["phase"] == "map"] != list(range(1, row["parts"] + 1)):
        problems.append("parts out of order")
    if [h["n"] for h in handoffs] != list(range(1, len(handoffs) + 1)):
        problems.append("hand-off numbering inconsistent")
    if len(handoffs) > row["cap"]:
        problems.append("hand-off count %d > cap %d" % (len(handoffs), row["cap"]))
    if len(results) != 1:
        problems.append("controller result missing" if not results else "several controller results")
    else:
        result = results[0]
        if (result.get("outcome") != "complete" or result.get("covered") != row["parts"] or result.get("total") != row["parts"]
                or result.get("synthesis") not in ("answer", "salvaged")):
            problems.append("controller: %s (accepted %s of %s parts, synthesis %s)" % (
                result.get("outcome"), result.get("covered"), result.get("total"), result.get("synthesis")))
    return problems


def execute(label, directory):
    """One interactive native run with a hard request cap. The terminal belongs to the user: every consent is theirs
    (nothing is piped or answered here). The question is passed as the session's ONE first question, so nothing has to
    be typed or pasted and no attempt can be wasted; the answer and the notes are written by the app to new files."""
    import shutil
    import subprocess
    name, kib = RUNS[label]
    out, cap = Path(directory), run_rows()[label]["cap"]
    binary = shutil.which("ai-desktop")
    if binary is None:
        print("EXECUTE: ai-desktop not in PATH")
        return 2
    print("\n=== %s: %s, part_size %dKiB, --max-requests %d ===" % (label, name, kib, cap))
    print("Question (given to the app automatically; you type the consents): %s\n" % QUESTIONS[name])
    command = [binary, "material", "--v", "--only-whole", "--max-requests", str(cap), "--first-question", QUESTIONS[name],
               "--meta-log", str(out / ("meta-%s.jsonl" % label)), "--answer-file", str(out / ("answer-%s.txt" % label)),
               str(out / "data" / (name + ".txt"))]
    try:
        code = subprocess.run(command).returncode
    except KeyboardInterrupt:
        code = 130
    (out / ("status-%s.txt" % label)).write_text("%d\n" % code)
    problems = check_run(label, out)
    if code != 0 or problems:
        print("RUN %s: INCOMPLETE - stopping the acceptance (evidence kept in %s): %s" % (label, out, "; ".join(problems) or "code %d" % code))
        return code if code else 1
    print("RUN %s: complete (code 0, phases and controller result match the plan)" % label)
    return 0


def check_config(expected_kib):
    from ai_desktop import permissions                  # read-only: the same loader the app uses
    policy = permissions.load_policy(optional=True)
    if policy is None:
        print("CONFIG: permissions.toml missing")
        return 3
    actual = policy.part_size()
    ok = actual == expected_kib * KIB
    print("CONFIG: part_size=%dKiB expected %dKiB -> %s" % (actual // KIB, expected_kib, "OK" if ok else "MISMATCH"))
    return 0 if ok else 3


def _records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _quality(path, expected):
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return None
    return score(path.read_text(encoding="utf-8"), expected)


def _cell(result):
    if result is None:
        return "none"
    return "%.2f/%.2f/%d/%d" % (result["identifier_recall"], result["value_correct"], len(result["wrong"]), len(result["fabricated"]))


def report(directory):
    out, rows, identities, limited, failed = Path(directory), run_rows(), set(), [], []
    print("%-9s %-16s %5s %4s %-20s %-17s %-17s %s" % ("run", "dataset", "req.", "cap", "transport",
                                                        "answer r/v/w/f", "notes r/v/w/f", "code"))
    for label, (name, kib) in RUNS.items():
        log, code = out / ("meta-%s.jsonl" % label), out / ("status-%s.txt" % label)
        handoffs = split_log(log)[0] if log.exists() else []
        row, expected = rows[label], build(name)[1]
        problems = check_run(label, out)
        entries = [e for r in handoffs for e in r["models"]]
        if any(e is None for e in entries):                      # missing metadata limits the QUALITY comparison only
            limited.append("%s: metadata missing in %d of %d requests" % (label, sum(e is None for e in entries), len(entries)))
        identities |= {(e["model"], e["window"]) for e in entries if e is not None}
        answer = _quality(out / ("answer-%s.txt" % label), expected)
        notes = _quality(out / ("answer-%s.txt.notes" % label), expected)
        if problems:
            failed.append("%s: %s" % (label, "; ".join(problems)))
            limited.append("%s: transport incomplete" % label)
        if answer is None:
            limited.append("%s: final answer missing" % label)
        if notes is None:
            limited.append("%s: notes missing" % label)
        print("%-9s %-16s %5d %4d %-20s %-17s %-17s %s" % (label, name, len(handoffs), row["cap"], "INCOMPLETE/MISMATCH" if problems else "OK",
              _cell(answer), _cell(notes), code.read_text().strip() if code.exists() else "?"))
    if len(identities) > 1:
        limited.append("model or window changed across runs: %s" % sorted(identities))
    print("\n(r/v/w/f = identifier recall / correct values / wrong / fabricated; links: see score)")
    print("TRANSPORT: %s" % ("OK in all four runs" if not failed else "FAILED - " + " | ".join(failed)))
    print("MODEL/WINDOW: %s" % (", ".join("%s window %s" % pair for pair in sorted(identities)) or "not available"))
    print("QUALITY COMPARISON: %s" % ("comparable (all four runs, one model and window)" if not limited and identities
                                      else "LIMITED - " + ("; ".join(limited) or "metadata missing")))
    print("This is a preliminary comparison of two datasets (4 runs), not proof of a general advantage of 512 KiB; the default part size stays 240 KiB.")
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    plan_cmd = sub.add_parser("plan")
    plan_cmd.add_argument("--limit", type=int, default=None, help="fail when the sum of caps exceeds LIMIT")
    write = sub.add_parser("write")
    write.add_argument("directory")
    scored = sub.add_parser("score")
    scored.add_argument("dataset", choices=sorted(DATASETS))
    scored.add_argument("answer_file")
    cfg = sub.add_parser("check-config")
    cfg.add_argument("expected_kib", type=int)
    for name in ("cap", "run", "question"):
        sub.add_parser(name).add_argument("label", choices=sorted(RUNS))
    labels = sub.add_parser("labels")
    labels.add_argument("kib", type=int, choices=sorted({k for _, k in RUNS.values()}))
    execute_cmd = sub.add_parser("execute")
    execute_cmd.add_argument("label", choices=sorted(RUNS))
    execute_cmd.add_argument("directory")
    rep = sub.add_parser("report")
    rep.add_argument("directory")
    args = parser.parse_args(argv)
    if args.cmd == "plan":
        rows = run_rows()
        print("Three different quantities: data_bytes = dynamic data (part text + notes 3 KiB x parts + history);")
        print("packet_bytes_max = UPPER BOUND of the serialized application packets (real questions, initial request, maps,")
        print("synthesis with maximal notes made only of \"); the client's real network traffic (TLS) is NOT measured.")
        print("cap = plan requests + 1 initial = the --max-requests value.\n")
        print("%-8s %-17s %-7s %5s %5s %6s %12s %16s %4s" % ("run", "dataset", "profile", "parts", "req.", "stages", "data_bytes", "packet_bytes_max", "cap"))
        total = {"cap": 0, "data": 0, "packet": 0}
        for label, (name, kib) in RUNS.items():
            row = rows[label]
            if row.get("refused"):
                print("%-8s %-17s refused before sending" % (label, name))
                return 1
            print("%-8s %-17s %-7s %5d %5d %6d %12d %16d %4d" % (label, name, "%dKiB" % kib, row["parts"], row["requests"], row["stages"],
                                                                row["data_bytes"], row["packet_bytes_max"], row["cap"]))
            total["cap"] += row["cap"]; total["data"] += row["data_bytes"]; total["packet"] += row["packet_bytes_max"]
        print("%-8s %-17s %-7s %5s %5s %6s %12d %16d %4d" % ("TOTAL", "", "", "", "", "", total["data"], total["packet"], total["cap"]))
        if args.limit is not None and total["cap"] > args.limit:
            print("LIMIT EXCEEDED: %d > %d" % (total["cap"], args.limit))
            return 1
    elif args.cmd == "write":
        out = Path(args.directory)
        (out / "data").mkdir(parents=True, exist_ok=True)
        (out / "expected").mkdir(parents=True, exist_ok=True)
        for name in DATASETS:
            raw, expected = build(name)
            (out / "data" / (name + ".txt")).write_bytes(raw)
            (out / "expected" / (name + ".json")).write_text(json.dumps(expected, ensure_ascii=False, indent=1))
        (out / "questions.json").write_text(json.dumps(QUESTIONS, ensure_ascii=False, indent=1))
        print("saved %d datasets: data in %s, expected answers in %s (outside what the model may read)" % (
            len(DATASETS), out / "data", out / "expected"))
    elif args.cmd == "score":
        _, expected = build(args.dataset)
        print(json.dumps(score(Path(args.answer_file).read_text(encoding="utf-8"), expected), ensure_ascii=False, indent=1))
    elif args.cmd == "check-config":
        return check_config(args.expected_kib)
    elif args.cmd == "labels":
        print("\n".join(label for label, (_, k) in RUNS.items() if k == args.kib))
    elif args.cmd == "execute":
        return execute(args.label, args.directory)
    elif args.cmd == "cap":
        print(run_rows()[args.label]["cap"])
    elif args.cmd == "run":
        print("%s %d" % RUNS[args.label])
    elif args.cmd == "question":
        print(QUESTIONS[RUNS[args.label][0]])
    else:
        return report(args.directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
