"""Tests for P6.4 (#182): Verify the client installation and setup tutorial."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from configure_github_repo import ApiError
from scripts import cli, doctor, headless, interactive, interactive_apply, setup_github_repo
from test_permission_degradation import CleanBaseApi
from test_setup_github_repo import InventoryApi

README_PATH = ROOT_DIR / "README.md"
P0_DOC_PATH = ROOT_DIR / "docs" / "p0-science-client-validation.md"
P1_DOC_PATH = ROOT_DIR / "docs" / "p1-unified-setup-validation.md"
INSTALL_SH = ROOT_DIR / "install.sh"
GH_SETUP_BIN = ROOT_DIR / "gh-setup"

from tests.p6_test_support import extract_readme_section


class P6ClientSetupTutorialDocumentationTests(unittest.TestCase):
    """Verify README.md '## Client setup' and validation docs meet P6.4 (#182) acceptance criteria."""

    def test_client_setup_section_covers_all_required_topics_and_exact_prompts(self):
        readme = README_PATH.read_text(encoding="utf-8")
        section = extract_readme_section(readme, "Client setup")
        self.assertTrue(section, "README.md must contain a '## Client setup' section")

        required_literals = (
            # Prerequisites & token scopes
            "3.10",
            "git",
            "gh",
            "repo",
            "workflow",
            "read:org",
            "project",
            "Administration",
            "Contents",
            "Workflows",
            "Pull requests",
            "Issues",
            "Metadata",
            # Installation & environment overrides
            "install.sh",
            "GH_SETUP_INSTALL_DIR",
            "GH_SETUP_BIN_DIR",
            "GITHUB_TOKEN",
            "gh auth status",
            # Diagnostics
            "gh-setup doctor",
            "--repo OWNER/REPO",
            "--json",
            "[CONFIGURED]",
            "[MISSING]",
            "[UNSUPPORTED]",
            "[INACCESSIBLE]",
            # Interactive Setup prompts & stages
            "Target repository (OWNER/REPO): ",
            "1. Resume existing setup",
            "2. Start from scratch",
            ".local/setup-policy.json",
            "Dry-Run Summary: Proposed Changes for",
            "Apply these changes? [y/N]: ",
            ".local/setup-dry-run.json",
            # Headless Mode & Setup PR
            "gh-setup dry-run --policy",
            "gh-setup apply --policy",
            "--dry-run-result",
            "codex/workflows-",
            # Repeat convergence & drift / partial-run recovery
            "No changes needed. Repository is already in the desired state.",
            "Repository state changed since the accepted Dry-Run; inspect a new Dry-Run before applying.",
        )
        for literal in required_literals:
            self.assertIn(
                literal,
                section,
                f"README.md '## Client setup' must include '{literal}'",
            )

    def test_each_step_in_client_setup_identifies_changes_manual_work_and_verification(self):
        readme = README_PATH.read_text(encoding="utf-8")
        section = extract_readme_section(readme, "Client setup")
        subsections = re.split(r"^###\s+", section, flags=re.MULTILINE)[1:]
        self.assertGreaterEqual(
            len(subsections),
            5,
            "README.md '## Client setup' must have structured ### subsections for the setup lifecycle",
        )
        for sub in subsections:
            heading = sub.splitlines()[0].strip()
            self.assertIn(
                "**What the client changes:**",
                sub,
                f"Subsection '### {heading}' must explicitly state '**What the client changes:**'",
            )
            self.assertIn(
                "**What remains for the maintainer to do in GitHub:**",
                sub,
                f"Subsection '### {heading}' must explicitly state '**What remains for the maintainer to do in GitHub:**'",
            )
            self.assertIn(
                "**How to verify success:**",
                sub,
                f"Subsection '### {heading}' must explicitly state '**How to verify success:**'",
            )

    def test_validation_docs_record_observed_inferred_and_unverified_evidence(self):
        self.assertFalse(P0_DOC_PATH.exists(), "p0-science-client-validation.md must be retired")
        self.assertFalse(P1_DOC_PATH.exists(), "p1-unified-setup-validation.md must be retired")
        readme = README_PATH.read_text(encoding="utf-8")
        for marker in ("**Observed**", "**Inferred**", "**Unverified**"):
            self.assertIn(
                marker,
                readme,
                f"README.md must record evidence using the {marker} classification",
            )


class P6ClientSetupTutorialExecutionTests(unittest.TestCase):
    """Exercise the documented client commands against disposable environments and fake APIs."""

    def _create_isolated_bin(self, base_dir: Path) -> Path:
        mock_dir = base_dir / "mock_bin"
        mock_dir.mkdir(exist_ok=True)
        for utility in ("bash", "env", "mkdir", "dirname", "ln", "chmod", "git", "python3"):
            real_path = shutil.which(utility)
            if real_path:
                sym = mock_dir / utility
                if not sym.exists():
                    sym.symlink_to(real_path)
        gh_mock = mock_dir / "gh"
        gh_mock.write_text(
            '#!/bin/sh\nif [ "$1" = "auth" ] && [ "$2" = "status" ]; then echo "Logged in"; exit 0; fi\nexit 0\n',
            encoding="utf-8",
        )
        gh_mock.chmod(gh_mock.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return mock_dir

    def test_install_sh_to_tempdir_and_doctor_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            install_dir = tmp_path / "share" / "gh-setup"
            bin_dir = tmp_path / "bin"
            mock_dir = self._create_isolated_bin(tmp_path)

            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(tmp_path),
                    "GH_SETUP_REPO_URL": str(ROOT_DIR),
                    "GH_SETUP_INSTALL_DIR": str(install_dir),
                    "GH_SETUP_BIN_DIR": str(bin_dir),
                    "PATH": f"{bin_dir}:{mock_dir}",
                }
            )
            proc = subprocess.run([str(INSTALL_SH)], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            installed_bin = bin_dir / "gh-setup"
            self.assertTrue(installed_bin.is_symlink())

            doc_proc = subprocess.run([str(installed_bin), "doctor"], env=env, capture_output=True, text=True)
            self.assertEqual(doc_proc.returncode, 0, doc_proc.stderr)
            self.assertIn("Verdict: System is healthy and ready for gh-setup.", doc_proc.stdout)

    def test_doctor_repo_classifies_capabilities_and_guided_fallbacks(self):
        api = CleanBaseApi(visibility="private", plan="free")
        out_human = io.StringIO()
        err_human = io.StringIO()
        code_human = doctor.run_doctor(
            out_stream=out_human,
            err_stream=err_human,
            repo="acme/project",
            json_output=False,
            api=api,
        )
        self.assertEqual(code_human, 1)
        report_text = out_human.getvalue()
        self.assertIn("[CONFIGURED]", report_text)
        self.assertIn("[MISSING]", report_text)
        self.assertIn("[UNSUPPORTED]", report_text)
        self.assertIn("Action:", report_text)

        out_json = io.StringIO()
        code_json = doctor.run_doctor(
            out_stream=out_json,
            err_stream=io.StringIO(),
            repo="acme/project",
            json_output=True,
            api=api,
        )
        self.assertEqual(code_json, 1)
        payload = json.loads(out_json.getvalue())
        self.assertEqual(payload["repository"], "acme/project")
        self.assertEqual(len(payload["controls"]), 10)

    def test_interactive_and_headless_convergence_drift_rejection_and_partial_run_recovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            policy_path = tmp_path / ".local" / "setup-policy.json"
            dry_run_path = tmp_path / ".local" / "setup-dry-run.json"

            api = CleanBaseApi(visibility="private", plan="pro")
            api.repo["has_issues"] = False
            api.repo["has_wiki"] = False

            policy = {
                "version": 2,
                "repository": "acme/project",
                "stages": [
                    {"name": "features", "enable": ["issues", "wiki"]},
                    {
                        "name": "workflows",
                        "workflows": [
                            {
                                "template": "pr-ci",
                                "template_version": 1,
                                "filename": "ci.yml",
                                "triggers": ["pull_request"],
                                "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
                                "commands": ["python3 -m unittest discover -s tests -p 'test_*.py'"],
                            }
                        ],
                    },
                ],
            }
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(json.dumps(policy, indent=2), encoding="utf-8")

            # 1. Headless Dry-Run (zero writes)
            dr_out = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), redirect_stdout(dr_out):
                dr_exit = headless.run_headless_apply(
                    str(policy_path), repo="acme/project", token="mock-token", apply=False
                )
            self.assertEqual(dr_exit, 0)
            self.assertEqual(api.writes, [])
            dry_run_path.write_text(dr_out.getvalue(), encoding="utf-8")

            # 2. Simulate state drift between dry-run and apply -> blocked with exit 2 and zero writes
            api.repo["has_issues"] = True
            drift_out = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), redirect_stdout(drift_out):
                drift_exit = headless.run_headless_apply(
                    str(policy_path),
                    repo="acme/project",
                    token="mock-token",
                    apply=True,
                    dry_run_path=str(dry_run_path),
                )
            self.assertEqual(drift_exit, 2)
            self.assertEqual(api.writes, [])
            drift_data = json.loads(drift_out.getvalue())
            self.assertIn(
                "Repository state changed since the accepted Dry-Run; inspect a new Dry-Run before applying.",
                drift_data["changes"][0]["reason"],
            )

            # 3. Restore state, apply cleanly -> creates draft Setup PR and verifies direct settings
            api.repo["has_issues"] = False
            ap_out = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), redirect_stdout(ap_out):
                ap_exit = headless.run_headless_apply(
                    str(policy_path),
                    repo="acme/project",
                    token="mock-token",
                    apply=True,
                    dry_run_path=str(dry_run_path),
                )
            self.assertEqual(ap_exit, 0)
            ap_data = json.loads(ap_out.getvalue())
            self.assertEqual(ap_data["setup_pull_request"], "https://github.com/acme/project/pull/12")
            self.assertTrue(ap_data["setup_branch"].startswith("codex/workflows-"))

            # 4. Simulate owner merge of `.github/workflows/ci.yml` onto main, then verify repeat convergence
            staged_branch_file = next(k for k in api.files if k.endswith("/.github/workflows/ci.yml"))
            api.files["/repos/acme/project/contents/.github/workflows/ci.yml"] = api.files[staged_branch_file]

            session = interactive.Session(
                repository="acme/project",
                api=api,
                in_stream=io.StringIO("n\n"),
                out_stream=io.StringIO(),
                err_stream=io.StringIO(),
            )
            conv_exit = interactive_apply.run_interactive_apply(session, policy)
            self.assertEqual(conv_exit, 0)
            self.assertIn(
                "No changes needed. Repository is already in the desired state.",
                session.out_stream.getvalue(),
            )

            # 5. Partial-run / missing workflow scope failure surfaces explicit recovery guidance
            class PartialStagingApi(CleanBaseApi):
                def request(self, method, path, payload=None):
                    if method == "PUT" and "/contents/.github/workflows/" in path:
                        raise ApiError(
                            "PUT " + path + ": HTTP 404: refusing to allow a Personal Access Token to create or update workflow"
                        )
                    return super().request(method, path, payload)

            partial_api = PartialStagingApi(visibility="private", plan="pro")
            partial_dr = setup_github_repo.run_policy(partial_api, "acme/project", policy, apply=False)
            partial_ap = setup_github_repo.run_policy(
                partial_api, "acme/project", policy, apply=True, accepted_dry_run=partial_dr
            )
            self.assertIn("recovery", partial_ap)
            self.assertIn("Inspect the workflow staging branch", partial_ap["recovery"])
            self.assertTrue(partial_ap["setup_branch"].startswith("codex/workflows-"))


if __name__ == "__main__":
    unittest.main()
