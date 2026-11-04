"""The LC0 measurement script must run offline and report sane numbers on small fixtures."""
import unittest

from scripts import lc_measure


class MeasureSmokeTests(unittest.TestCase):
    def test_small_fixtures_report_all_fields_and_the_marker(self):
        results = [lc_measure.measure(name, raw) for name, raw in lc_measure.fixtures(size=65536)]
        self.assertEqual(len(results), 4)
        for result in results:
            self.assertLessEqual(result["bytes"], 65536)
            self.assertGreaterEqual(result["chunks"], 2)
            for key in ("import_ms", "chunk_plan_ms", "search_all_pages_ms", "peak_python_kib", "search_pages"):
                self.assertGreaterEqual(result[key], 0)
        self.assertEqual([r["hits"] for r in results], [1, 1, 0, 0])


if __name__ == "__main__":
    unittest.main()
