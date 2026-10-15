"""Run JavaScript state regressions in CI without a browser/network."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js required for frontend state tests")
class FrontendStateTests(unittest.TestCase):
    def test_versions_and_late_account_trip_responses(self):
        script = Path(__file__).with_name("frontend_state_check.js")
        result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
