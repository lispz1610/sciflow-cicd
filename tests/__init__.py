"""Test package.

Make top-level test helpers importable both with ``unittest discover -s tests``
and with package-style test names such as ``tests.test_configure_github_repo``.
"""

from pathlib import Path
import sys

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
