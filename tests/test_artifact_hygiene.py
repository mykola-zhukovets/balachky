"""Вартові проти тестових артефактів у робочому дереві."""
import unittest
from pathlib import Path


WORKTREE_ROOT = Path(__file__).resolve().parents[1]


class WorktreeArtifactGuardTests(unittest.TestCase):
    def test_target_suites_leave_no_component_model_in_worktree(self):
        unexpected = (
            WORKTREE_ROOT / "components" / "punctuator" / "model.bin",
        )
        leaked = [str(path.relative_to(WORKTREE_ROOT))
                  for path in unexpected if path.exists()]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
