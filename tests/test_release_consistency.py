"""One package version everywhere it is checked: PKGBUILD, runners, README, CHANGELOG."""
from pathlib import Path
import re
import unittest

import ai_desktop

ROOT = Path(__file__).resolve().parent.parent


def pkgbuild_version():
    text = (ROOT / "packaging/arch/PKGBUILD").read_text()
    ver = re.search(r"^pkgver=(\S+)$", text, re.M).group(1)
    rel = re.search(r"^pkgrel=(\d+)$", text, re.M).group(1)
    return ver + "-" + rel


class ReleaseConsistencyTests(unittest.TestCase):
    def test_package_module_and_pkgbuild_agree_on_the_upstream_version(self):
        self.assertEqual(pkgbuild_version().rsplit("-", 1)[0], ai_desktop.__version__)

    def test_acceptance_runners_check_exactly_the_pkgbuild_version(self):
        expected = "ai-desktop " + pkgbuild_version()
        runner = (ROOT / "scripts/accept_web_candidate.fish").read_text()
        clients = (ROOT / "scripts/accept_system_clients_candidate.fish").read_text()
        host = (ROOT / "tests/host_analysis_acceptance.py").read_text()
        self.assertEqual(re.findall(r"ai-desktop \d+\.\d+\.\d+-\d+", runner), [expected])
        self.assertEqual(re.findall(r"ai-desktop \d+\.\d+\.\d+-\d+", clients), [expected])
        self.assertEqual(re.findall(r"ai-desktop \d+\.\d+\.\d+-\d+", host), [expected])

    def test_readme_names_the_current_candidate(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("The current candidate package is **" + pkgbuild_version() + "**", readme)

    def test_changelog_leads_with_the_current_candidate_and_never_repeats_a_number(self):
        headings = re.findall(r"^## (\d+\.\d+\.\d+-\d+):", (ROOT / "CHANGELOG.md").read_text(), re.M)
        self.assertEqual(headings[0], pkgbuild_version())
        self.assertEqual(len(headings), len(set(headings)))
        order = [tuple(int(n) for n in re.split(r"[.-]", h)) for h in headings]
        self.assertEqual(order, sorted(order, reverse=True))

    def test_stable_version_is_not_claimed_before_the_gates_close(self):
        self.assertTrue(pkgbuild_version().startswith("0."))
        self.assertNotRegex((ROOT / "CHANGELOG.md").read_text(), r"(?m)^## 1\.0\.0")


if __name__ == "__main__":
    unittest.main()
