"""One-pass source-to-migration record verification."""
import sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from verify_reconstruction import reconstruct
from validate_redesign import jsonl

class ReconstructionTests(unittest.TestCase):
    def test_every_record_reconstructs_exactly(self):
        self.assertEqual(reconstruct(),jsonl(ROOT/'tasks.jsonl'))
