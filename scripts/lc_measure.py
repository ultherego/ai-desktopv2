#!/usr/bin/env python3
"""LC0 local measurements for materials: no model, no network, no files written.

Prints one JSON object per synthetic fixture: import, chunk plan and a full
paginated search (wall time in ms) plus peak Python allocation (tracemalloc).
Model/network latency and sudo/session start-up are NOT measured here.
"""
import json
from pathlib import Path
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai_desktop import materials as m  # noqa: E402

MARK = "ERROR-MARKER"


def fixtures(size=m.MAX_MATERIAL_BYTES):
    line = b"2026-10-31T12:00:00 INFO service heartbeat ok\n"
    log = (line * (size // len(line) + 1))[:size - 60] + b"\n" + MARK.encode() + b" code=END\n"
    yield "A1 log (many short lines, marker at the end)", log
    yield "A2 one single line", (b"x" * (size - 60) + MARK.encode() + b"\n")
    yield "A2 CRLF without final newline", (b"row of crlf text\r\n" * (size // 18))[:size - 12] + b"no newline"
    yield "multibyte (2/3/4-byte characters)", ("za\u017c\u00f3\u0142\u0107 日本語 🙂 " * (size // 20)).encode()[:size - 6].decode("utf-8", "ignore").encode()


def timed(function):
    started = time.perf_counter()
    value = function()
    return value, round((time.perf_counter() - started) * 1000, 2)


def measure(name, raw):
    tracemalloc.start()
    material, import_ms = timed(lambda: m.import_bytes(raw, "file"))
    plan, plan_ms = timed(lambda: m.chunk_plan(material))

    def full_search():
        hits, cursor, pages = 0, 0, 0
        while True:
            page = m.search_text(material, MARK, cursor)
            hits += len(page.hits)
            pages += 1
            if page.complete:
                return hits, pages
            cursor = page.next_cursor
    (hits, pages), search_ms = timed(full_search)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"fixture": name, "bytes": len(raw), "lines": len(material.line_starts), "chunks": len(plan),
            "import_ms": import_ms, "chunk_plan_ms": plan_ms, "search_all_pages_ms": search_ms,
            "search_pages": pages, "hits": hits, "peak_python_kib": peak // 1024}


def main():
    for name, raw in fixtures():
        print(json.dumps(measure(name, raw), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
