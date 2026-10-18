"""Offline JavaScript regression checks for text place confirmation."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class PlaceFrontendTests(unittest.TestCase):
    def test_verification_and_confirmation_interactions(self):
        result = subprocess.run(["node", str(Path(__file__).with_name("place_frontend_check.js"))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
