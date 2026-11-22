"""Tests for shared path security and symlink rejection utilities."""

import tempfile
import unittest
from pathlib import Path

from scripts.path_security import reject_symlink_components, safe_relative_path


class CustomPathError(Exception):
    pass


class PathSecurityTests(unittest.TestCase):
    def test_reject_symlink_components_rejects_dotdot_traversal_before_abspath(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real_dir = root / "real"
            real_dir.mkdir()
            symlink_dir = root / "link"
            symlink_dir.symlink_to(real_dir)

            # os.path.abspath(link/../real) would lexically collapse link/.. and miss the symlink
            traversal_path = symlink_dir / ".." / "real"
            with self.assertRaisesRegex(ValueError, r"\.\."):
                reject_symlink_components(traversal_path)

            with self.assertRaises(CustomPathError):
                reject_symlink_components("a/../b", error_cls=CustomPathError)

    def test_reject_symlink_components_rejects_symlink_ancestor_and_leaf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target)

            with self.assertRaises(ValueError):
                reject_symlink_components(link)

            with self.assertRaises(ValueError):
                reject_symlink_components(link / "child.txt")

            # include_leaf=False allows symlink leaf if ancestors are clean
            reject_symlink_components(link, include_leaf=False)

    def test_safe_relative_path_validates_posix_paths(self):
        self.assertEqual(safe_relative_path("a/b/c.txt"), Path("a/b/c.txt"))
        with self.assertRaises(ValueError):
            safe_relative_path("../secret")
        with self.assertRaises(ValueError):
            safe_relative_path("/abs/path")


if __name__ == "__main__":
    unittest.main()
