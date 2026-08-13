import tempfile
import unittest
from pathlib import Path

from actions.file_controller import delete_file, file_controller


class TestFileControllerSafety(unittest.TestCase):
    def test_delete_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "sample.txt"
            target.write_text("hello", encoding="utf-8")

            result = delete_file(str(target), confirm=False)

            self.assertIn("confirm", result.lower())
            self.assertTrue(target.exists())

    def test_wrapper_blocks_destructive_action_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "sample.txt"
            target.write_text("hello", encoding="utf-8")

            result = file_controller(
                {
                    "action": "delete",
                    "path": tmpdir,
                    "name": "sample.txt",
                }
            )

            self.assertIn("confirm", result.lower())
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
