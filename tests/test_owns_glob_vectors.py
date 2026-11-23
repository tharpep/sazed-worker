import json
import unittest
from pathlib import Path

from sazed_worker.dispatch.rules import glob_match

VECTORS = Path(__file__).parent / "data" / "owns_glob_vectors.json"


class OwnsGlob(unittest.TestCase):
    def test_glob_match_agrees_with_the_vectors_sazed_runs(self):
        """The same file as sazed's tests/data, so the export gate and the engine agree on owns."""
        for path, pattern, want in json.loads(VECTORS.read_text(encoding="utf-8")):
            with self.subTest(path=path, pattern=pattern):
                self.assertIs(glob_match(path, pattern), want)


if __name__ == "__main__":
    unittest.main()
