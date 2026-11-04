"""Real kernel/TLS test. No Claude, OAuth, uplink or writable host filesystem."""
import unittest

from ai_desktop.e2_subscription_boundary import CHECKS, run_synthetic


class SubscriptionBoundaryChecks(unittest.TestCase):
    def test_external_proxy_and_host_denials(self):
        result = run_synthetic()
        self.assertEqual(result["checks"], dict.fromkeys(CHECKS, True))
        self.assertIs(result["real_client"], False)
        self.assertIs(result["real_auth"], False)
        self.assertIs(result["upstream"], False)


if __name__ == "__main__":
    unittest.main()
