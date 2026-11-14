"""Tests for audit baseline and contracts matrix (Issue #12)."""

import re
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT_DIR / "docs" / "audit-baseline-matrix.md"


class AuditBaselineMatrixTests(unittest.TestCase):
    def test_matrix_file_exists(self):
        self.assertTrue(
            MATRIX_PATH.exists(),
            f"Audit baseline and contracts matrix must exist at {MATRIX_PATH}",
        )

    def test_baseline_commit_and_environment_recorded(self):
        content = MATRIX_PATH.read_text(encoding="utf-8")
        # Baseline commit recorded
        self.assertIn("3e3ed40c3d5a286fbc09bc079f05fc739ee3fe02", content)
        # Python and platform recorded
        self.assertIn("Python", content)
        self.assertIn("3.12", content)
        self.assertIn("Linux", content)

    def test_representative_public_flow_recorded_with_exit_status(self):
        content = MATRIX_PATH.read_text(encoding="utf-8")
        self.assertIn("Representative Public Workflow", content)
        self.assertIn("gh-setup", content)
        self.assertIn("Exit status: 0", content)

    def test_capability_matrix_covers_all_audit_subsystems(self):
        content = MATRIX_PATH.read_text(encoding="utf-8")
        expected_subsystems = [
            "Installation & CLI",
            "Interactive Setup",
            "Headless Mode & Policy Contracts",
            "Repository Settings",
            "Security & CodeQL",
            "Managed Content, Projects V2 & Wiki",
            "Setup Pull Request Workflow Staging",
            "Review Setup & Advisory Review",
            "Merge Gates & Approval Evidence",
            "CI & Automated Testing",
        ]
        for subsystem in expected_subsystems:
            self.assertIn(
                subsystem,
                content,
                f"Capability matrix must include subsystem '{subsystem}'",
            )

    def test_matrix_distinguishes_commitments_limitations_and_historical(self):
        content = MATRIX_PATH.read_text(encoding="utf-8")
        self.assertIn("Version Commitments", content)
        self.assertIn("Declared Limitations", content)
        self.assertIn("Historical Items", content)

    def test_reproducible_findings_documented(self):
        content = MATRIX_PATH.read_text(encoding="utf-8")
        self.assertIn("Reproducible Findings", content)
        # CI Python version coverage gap noted
        self.assertIn("Python 3.10", content)


if __name__ == "__main__":
    unittest.main()
