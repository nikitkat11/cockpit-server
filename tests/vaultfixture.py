"""
A throwaway vault on disk, so the parser tests run against real files rather
than mocks. The parsers take a directory and read it; testing them through
anything other than a directory would be testing a different function.
"""

import os
import shutil
import sys
import tempfile
import unittest

# import the server module from the repo root, whatever the cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cockpit  # noqa: E402


class VaultTestCase(unittest.TestCase):
    """Point cockpit.VAULT at a temp directory for the duration of one test.

    cockpit.VAULT is a module-level global resolved at import time. That is a
    real testability wart in the server — there is no way to construct a parser
    bound to a different directory — and this fixture is the seam that works
    around it. Written down rather than hidden: if the parsers ever took the
    vault as an argument, this class would disappear.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cockpit-test-")
        self._saved_vault = cockpit.VAULT
        cockpit.VAULT = self.dir

    def tearDown(self):
        cockpit.VAULT = self._saved_vault
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, relpath, text):
        """Create a note at `relpath`, making parent folders as needed."""
        path = os.path.join(self.dir, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def read(self, relpath):
        with open(os.path.join(self.dir, relpath), encoding="utf-8") as f:
            return f.read()
