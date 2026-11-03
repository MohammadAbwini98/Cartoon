"""No credential may be tracked by git (what a push would publish)."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Secrets(unittest.TestCase):
    def test_tracked_files_are_clean(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_secrets.py"), ROOT],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_gitignore_covers_worker_secrets(self):
        for path in ("secrets.json", "x.key", ".env", "state/authorization.json", "media/source/ep.mp4"):
            r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", path])
            self.assertEqual(r.returncode, 0, f"{path} would be committable")


if __name__ == "__main__":
    unittest.main()
