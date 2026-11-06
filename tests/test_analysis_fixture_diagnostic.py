"""The disposable-CI diagnostic uses and closes the real binding interface."""
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

import analysis_fixture_diagnostic as diagnostic
from ai_desktop.analysis_contract import AnalysisDenied
from claude_client_fixtures import compatibility_results
from system_client_fixtures import FakeBinding


class AnalysisFixtureDiagnosticTests(unittest.TestCase):
    def test_all_offline_probes_share_descriptors_and_close_after_success(self):
        bound = FakeBinding("claude", pass_fds=(17, 19))
        phases = ("version", "compat-options", "compat-web", "compat-reject", "compat-rpc")
        with patch.object(diagnostic.client_binding, "bind", return_value=bound) as bind, \
                patch.object(diagnostic, "namespace_command", side_effect=lambda action, *args, **kwargs: [action]) as command, \
                patch.object(diagnostic, "bounded_process", side_effect=compatibility_results()) as native, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diagnostic.main(), 0)
        bind.assert_called_once_with("claude")
        self.assertEqual([call.args[0] for call in command.call_args_list], list(phases))
        for call in command.call_args_list:
            self.assertEqual(call.kwargs, {"online": False, "bound": bound})
        for call in native.call_args_list:
            self.assertEqual(call.kwargs, {"pass_fds": (17, 19), "env": {"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                                           "timeout": 10, "limit": 4096})
        self.assertTrue(bound.closed)
        self.assertEqual(bound.rechecks, len(phases))
        self.assertIn("CI SYNTHETIC COMPATIBILITY: PASS", output.getvalue())

    def test_failed_probe_stops_and_closes_binding_without_exception_text(self):
        for failure in (AnalysisDenied("client-changed"), OSError("SECRET")):
            bound = FakeBinding("claude", pass_fds=(17,))
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(diagnostic.client_binding, "bind", return_value=bound), \
                    patch.object(diagnostic, "namespace_command", return_value=["version"]), \
                    patch.object(diagnostic, "bounded_process", side_effect=failure) as native, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(diagnostic.main(), 2)
            native.assert_called_once()
            self.assertTrue(bound.closed)
            self.assertIn("FAIL phase=version", output.getvalue())
            self.assertNotIn("SECRET", output.getvalue())

    def test_binding_refusal_does_not_launch_a_client(self):
        with patch.object(diagnostic.client_binding, "bind", side_effect=AnalysisDenied("client-unsafe")), \
                patch.object(diagnostic, "bounded_process") as native, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diagnostic.main(), 2)
        native.assert_not_called()
        self.assertIn("FAIL phase=binding reason=client-unsafe", output.getvalue())


if __name__ == "__main__":
    unittest.main()
