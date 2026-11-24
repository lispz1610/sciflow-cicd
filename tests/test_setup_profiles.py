"""Unit and integration tests for Setup Profile catalog and archetype recommendation engine."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from scripts.setup_profiles import (
        CANONICAL_TRIAGE_LABELS,
        COMMON_BASELINE_STAGES,
        PROFILE_CATALOG,
        Profile,
        get_profile,
        list_profiles,
        profile_to_policy,
        recommend_profile,
    )
    from scripts.setup_github_repo import validate_policy
except ImportError:
    from setup_profiles import (
        CANONICAL_TRIAGE_LABELS,
        COMMON_BASELINE_STAGES,
        PROFILE_CATALOG,
        Profile,
        get_profile,
        list_profiles,
        profile_to_policy,
        recommend_profile,
    )
    from setup_github_repo import validate_policy


class SetupProfileCatalogTests(unittest.TestCase):
    """Test setup profile catalog definitions, lookups, and baseline inheritance."""

    def test_catalog_contains_six_canonical_profiles(self):
        expected_ids = {
            "scientific-python",
            "hpc-cmake-mpi",
            "agent-ready-governance",
            "containerized-scientific",
            "full-suite",
            "minimal-lean",
        }
        profiles = list_profiles()
        profile_ids = {p.id for p in profiles}
        self.assertEqual(profile_ids, expected_ids)
        self.assertEqual(len(profiles), 6)

    def test_get_profile_by_id(self):
        for profile_id in [
            "scientific-python",
            "hpc-cmake-mpi",
            "agent-ready-governance",
            "containerized-scientific",
            "full-suite",
            "minimal-lean",
        ]:
            p = get_profile(profile_id)
            self.assertIsInstance(p, Profile)
            self.assertEqual(p.id, profile_id)
            self.assertTrue(p.display_name)
            self.assertTrue(p.description)

    def test_get_profile_invalid_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            get_profile("nonexistent-profile")

    def test_canonical_triage_labels(self):
        label_names = [label["name"] for label in CANONICAL_TRIAGE_LABELS]
        self.assertEqual(
            label_names,
            ["needs-triage", "bug", "enhancement", "documentation", "ready-for-agent"],
        )
        for label in CANONICAL_TRIAGE_LABELS:
            self.assertIn("name", label)
            self.assertIn("color", label)
            self.assertIn("description", label)
            self.assertEqual(len(label["color"]), 6)

    def test_universal_common_baseline_stages(self):
        baseline = COMMON_BASELINE_STAGES
        stage_names = [stage["name"] for stage in baseline]
        self.assertIn("features", stage_names)
        self.assertIn("merge", stage_names)
        self.assertIn("actions-policy", stage_names)
        self.assertIn("security", stage_names)
        self.assertIn("labels", stage_names)

        # Features: issues enabled, wiki disabled per ADR 0005
        features_stage = next(s for s in baseline if s["name"] == "features")
        self.assertIn("issues", features_stage["enable"])
        self.assertNotIn("wiki", features_stage["enable"])
        self.assertIn("wiki", features_stage.get("disable", []))

        # Merge hygiene
        merge_stage = next(s for s in baseline if s["name"] == "merge")
        settings = merge_stage["settings"]
        self.assertTrue(settings["allow_squash_merge"])
        self.assertFalse(settings["allow_merge_commit"])
        self.assertFalse(settings["allow_rebase_merge"])
        self.assertTrue(settings["delete_branch_on_merge"])
        self.assertTrue(settings["allow_update_branch"])

        # Actions permissions: read-only
        actions_stage = next(s for s in baseline if s["name"] == "actions-policy")
        self.assertEqual(actions_stage["settings"]["default_workflow_permissions"], "read")

        # Security: vulnerability-alerts and automated-security-fixes
        sec_stage = next(s for s in baseline if s["name"] == "security")
        self.assertIn("vulnerability-alerts", sec_stage["enable"])
        self.assertIn("automated-security-fixes", sec_stage["enable"])

        # Labels: 5 canonical triage labels
        labels_stage = next(s for s in baseline if s["name"] == "labels")
        self.assertEqual(len(labels_stage["items"]), 5)


class RecommendationEngineTests(unittest.TestCase):
    """Test recommendation engine accuracy on sample repository markers and live inventory."""

    def test_recommend_python_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
            rec = recommend_profile(repo_dir=tmppath)
            self.assertEqual(rec.id, "scientific-python")

    def test_recommend_python_from_requirements(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "requirements.txt").write_text("numpy\nscipy\n", encoding="utf-8")
            rec = recommend_profile(repo_dir=tmppath)
            self.assertEqual(rec.id, "scientific-python")

    def test_recommend_hpc_cmake_mpi_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.20)\nfind_package(MPI REQUIRED)\n",
                encoding="utf-8",
            )
            rec = recommend_profile(repo_dir=tmppath)
            self.assertEqual(rec.id, "hpc-cmake-mpi")

    def test_recommend_containerized_scientific(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
            rec = recommend_profile(repo_dir=tmppath)
            self.assertEqual(rec.id, "containerized-scientific")

    def test_recommend_full_suite_when_container_and_agent_skills_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
            (tmppath / "AGENTS.md").write_text("# Agent instructions\n", encoding="utf-8")
            rec = recommend_profile(repo_dir=tmppath)
            self.assertEqual(rec.id, "full-suite")

    def test_recommend_agent_ready_governance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "AGENTS.md").write_text("# Agents documentation\n", encoding="utf-8")
            rec = recommend_profile(repo_dir=tmppath)
            self.assertEqual(rec.id, "agent-ready-governance")

    def test_recommend_minimal_lean_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "README.md").write_text("# My project\n", encoding="utf-8")
            rec = recommend_profile(repo_dir=tmppath)
            self.assertEqual(rec.id, "minimal-lean")

    def test_recommend_from_inventory_python(self):
        inventory = {
            "repository": {"language": "Python"},
            "languages": {"Python": 15000},
        }
        rec = recommend_profile(repo_dir=Path("/nonexistent/empty/dir"), inventory=inventory)
        self.assertEqual(rec.id, "scientific-python")

    def test_recommend_from_inventory_c_plus_plus(self):
        inventory = {
            "repository": {"language": "C++"},
            "languages": {"C++": 25000, "C": 5000},
        }
        rec = recommend_profile(repo_dir=Path("/nonexistent/empty/dir"), inventory=inventory)
        self.assertEqual(rec.id, "hpc-cmake-mpi")


class ProfileToPolicyTests(unittest.TestCase):
    """Test policy generation and schema validation across all profiles."""

    def test_all_profiles_produce_valid_v2_policy(self):
        for profile in list_profiles():
            policy = profile_to_policy(profile, repository="octocat/hello-world")
            self.assertEqual(policy["version"], 2)
            self.assertEqual(policy["repository"], "octocat/hello-world")
            stages = validate_policy(policy)
            self.assertTrue(len(stages) >= 5)

            # Ensure baseline stages are present in every policy
            stage_names = {s["name"] for s in stages}
            self.assertTrue({"features", "merge", "actions-policy", "security", "labels"}.issubset(stage_names))

    def test_profile_to_policy_with_string_id(self):
        policy = profile_to_policy("minimal-lean", repository="octocat/hello-world")
        self.assertEqual(policy["version"], 2)
        stages = validate_policy(policy)
        self.assertEqual(len(stages), 5)

    def test_scientific_python_policy_structure(self):
        policy = profile_to_policy("scientific-python", repository="octocat/hello-world")
        stage_names = {s["name"] for s in policy["stages"]}
        self.assertIn("workflows", stage_names)
        self.assertIn("codeql", stage_names)

        ci_stage = next(w for s in policy["stages"] if s["name"] == "workflows" for w in s["workflows"] if w.get("template") == "profile-ci")
        self.assertEqual(ci_stage["profile"], "python")
        self.assertTrue(ci_stage["python"]["cache"])
        self.assertTrue(ci_stage["python"]["lfs"])

    def test_hpc_cmake_mpi_policy_structure(self):
        policy = profile_to_policy("hpc-cmake-mpi", repository="octocat/hello-world")
        stage_names = {s["name"] for s in policy["stages"]}
        self.assertIn("workflows", stage_names)
        self.assertIn("codeql", stage_names)

        ci_stage = next(w for s in policy["stages"] if s["name"] == "workflows" for w in s["workflows"] if w.get("template") == "profile-ci")
        self.assertEqual(ci_stage["profile"], "cmake-mpi")
        self.assertEqual(ci_stage["cmake-mpi"]["ranks"], [1, 2])
        self.assertTrue(ci_stage["cmake-mpi"]["ccache"])

    def test_agent_ready_governance_policy_structure(self):
        policy = profile_to_policy("agent-ready-governance", repository="octocat/hello-world")
        stage_names = {s["name"] for s in policy["stages"]}
        self.assertIn("agent-skills", stage_names)
        self.assertIn("issue-state-gate", stage_names)

        skills_stage = next(s for s in policy["stages"] if s["name"] == "agent-skills")
        self.assertEqual(skills_stage["bundle"], "mattpocock")

    def test_containerized_scientific_policy_structure(self):
        policy = profile_to_policy("containerized-scientific", repository="octocat/hello-world")
        stage_names = {s["name"] for s in policy["stages"]}
        self.assertIn("workflows", stage_names)

        wf_stage = next(s for s in policy["stages"] if s["name"] == "workflows")
        templates = {w["template"] for w in wf_stage["workflows"]}
        self.assertIn("container-publish", templates)
        self.assertIn("dependency-review", templates)

    def test_full_suite_policy_structure(self):
        policy = profile_to_policy("full-suite", repository="octocat/hello-world")
        stage_names = {s["name"] for s in policy["stages"]}
        self.assertIn("workflows", stage_names)
        self.assertIn("agent-skills", stage_names)
        self.assertIn("issue-state-gate", stage_names)

        wf_stage = next(s for s in policy["stages"] if s["name"] == "workflows")
        templates = {w["template"] for w in wf_stage["workflows"]}
        self.assertIn("container-publish", templates)
        self.assertIn("dependency-review", templates)

    def test_uv_package_manager_detection(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "uv.lock").touch()
            (tmp_path / "pyproject.toml").write_text('[tool.uv]\ndev-dependencies = []\n', encoding="utf-8")

            # scientific-python detects uv
            policy = profile_to_policy("scientific-python", repository="octocat/hello-world", repo_dir=tmp_path)
            ci_stage = next(w for s in policy["stages"] if s["name"] == "workflows" for w in s["workflows"] if w.get("template") == "profile-ci")
            self.assertEqual(ci_stage["python"]["package_manager"], "uv")

            # containerized-scientific detects python + uv
            policy_cont = profile_to_policy("containerized-scientific", repository="octocat/hello-world", repo_dir=tmp_path)
            ci_cont = next(w for s in policy_cont["stages"] if s["name"] == "workflows" for w in s["workflows"] if w.get("template") == "profile-ci")
            self.assertEqual(ci_cont["python"]["package_manager"], "uv")

            # explicit override takes precedence
            policy_override = profile_to_policy(
                "scientific-python",
                repository="octocat/hello-world",
                repo_dir=tmp_path,
                options={"python": {"package_manager": "pip"}},
            )
            ci_override = next(w for s in policy_override["stages"] if s["name"] == "workflows" for w in s["workflows"] if w.get("template") == "profile-ci")
            self.assertEqual(ci_override["python"]["package_manager"], "pip")

    def test_profile_to_policy_populates_expected_sha_from_inventory(self):
        ci_sha = "a" * 40
        dep_sha = "b" * 40
        inv = {
            "managed_workflows": [
                {"filename": "ci.yml", "template": "profile-ci", "expected_sha": ci_sha},
                {"path": ".github/workflows/dependency-review.yml", "sha": dep_sha},
            ]
        }
        policy = profile_to_policy(
            "full-suite",
            repository="octocat/hello-world",
            inventory={**inv, "repository": {"language": "Python"}},
        )
        wf_stage = next(s for s in policy["stages"] if s["name"] == "workflows")
        by_filename = {w["filename"]: w for w in wf_stage["workflows"]}
        self.assertEqual(by_filename["ci.yml"].get("expected_sha"), ci_sha)
        self.assertEqual(by_filename["dependency-review.yml"].get("expected_sha"), dep_sha)
        self.assertNotIn("expected_sha", by_filename["container-publish.yml"])

    def test_profile_to_policy_preserves_observed_action_pins_unless_overridden(self):
        bumped_checkout = "c" * 40
        bumped_setup_python = "d" * 40
        inv = {
            "repository": {"language": "Python"},
            "managed_workflows": [
                {
                    "filename": "ci.yml",
                    "template": "profile-ci",
                    "profile": "python",
                    "expected_sha": "a" * 40,
                    "checkout_sha": bumped_checkout,
                    "setup_python_sha": bumped_setup_python,
                }
            ],
        }
        policy = profile_to_policy("scientific-python", repository="octocat/hello-world", inventory=inv)
        ci_wf = next(
            w for s in policy["stages"] if s["name"] == "workflows"
            for w in s["workflows"] if w["filename"] == "ci.yml"
        )
        self.assertEqual(ci_wf["checkout_sha"], bumped_checkout)
        self.assertEqual(ci_wf["setup_python_sha"], bumped_setup_python)

        override_checkout = "e" * 40
        override_setup_python = "f" * 40
        policy_overridden = profile_to_policy(
            "scientific-python",
            repository="octocat/hello-world",
            inventory=inv,
            options={"python": {"checkout_sha": override_checkout, "setup_python_sha": override_setup_python}},
        )
        ci_overridden = next(
            w for s in policy_overridden["stages"] if s["name"] == "workflows"
            for w in s["workflows"] if w["filename"] == "ci.yml"
        )
        self.assertEqual(ci_overridden["checkout_sha"], override_checkout)
        self.assertEqual(ci_overridden["setup_python_sha"], override_setup_python)

    def test_round_trip_dependabot_pins_across_parse_defaults_profile_and_render(self):
        import hashlib
        from scripts.install_github_workflows import render
        from scripts.interactive_defaults import extract_defaults_from_inventory
        from scripts.setup_github_repo import parse_workflow_content

        bumped_checkout = "1" * 40
        bumped_setup_python = "2" * 40
        initial_bytes = render({
            "template": "profile-ci",
            "template_version": 1,
            "profile": "scientific-python",
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": bumped_checkout,
            "setup_python_sha": bumped_setup_python,
            "python": {"versions": ["3.12"], "commands": ["pytest -q"]},
        })
        blob_sha = hashlib.sha1(b"blob " + str(len(initial_bytes)).encode() + b"\0" + initial_bytes).hexdigest()

        parsed = parse_workflow_content(".github/workflows/ci.yml", initial_bytes, blob_sha)
        defaults = extract_defaults_from_inventory({"managed_workflows": [parsed]})
        self.assertEqual(defaults["python"]["checkout_sha"], bumped_checkout)
        self.assertEqual(defaults["python"]["setup_python_sha"], bumped_setup_python)

        policy = profile_to_policy(
            "scientific-python",
            repository="octocat/hello-world",
            inventory={"workflow_files": {".github/workflows/ci.yml": initial_bytes.decode("utf-8")}},
        )
        ci_wf = next(
            w for s in policy["stages"] if s["name"] == "workflows"
            for w in s["workflows"] if w["filename"] == "ci.yml"
        )
        rerendered = render(ci_wf).decode("utf-8")
        self.assertIn(f"actions/checkout@{bumped_checkout}", rerendered)
        self.assertIn(f"actions/setup-python@{bumped_setup_python}", rerendered)

    def test_profile_to_policy_propagates_suite_and_dependabot_shas(self):
        gate_shas = {
            ".github/workflows/issue-state-gate.yml": "1" * 40,
            ".github/workflows/issue-state-gate-refresh.yml": "2" * 40,
            "scripts/issue_state_gate.py": "3" * 40,
        }
        doc_shas = {
            ".github/workflows/documentation-freshness.yml": "4" * 40,
            "scripts/documentation_issue_reporter.py": "5" * 40,
            "docs/documentation-freshness.md": "6" * 40,
        }
        dep_sha = "7" * 40
        ci_sha = "8" * 40
        inv = {
            "repository": {"language": "Python"},
            "managed_workflows": [
                {
                    "filename": "ci.yml",
                    "template": "profile-ci",
                    "profile": "python",
                    "expected_sha": ci_sha,
                    "checkout_sha": "a" * 40,
                    "setup_python_sha": "b" * 40,
                }
            ],
            "managed_suites": {
                "issue-state-gate": {
                    "expected_sha": gate_shas[".github/workflows/issue-state-gate.yml"],
                    "expected_shas": gate_shas,
                },
                "documentation-freshness": {
                    "expected_sha": doc_shas[".github/workflows/documentation-freshness.yml"],
                    "expected_shas": doc_shas,
                },
            },
            "dependabot": {
                "status": "managed",
                "sha": dep_sha,
                "cadence": "weekly",
                "package_ecosystems": ["pip", "github-actions"],
                "open_pull_requests_limit": 5,
                "groups": True,
            },
        }

        policy = profile_to_policy("full-suite", repository="acme/project", inventory=inv)
        by_stage = {s["name"]: s for s in policy["stages"]}

        self.assertEqual(by_stage["issue-state-gate"]["expected_sha"], gate_shas[".github/workflows/issue-state-gate.yml"])
        self.assertEqual(by_stage["issue-state-gate"]["expected_shas"], gate_shas)
        self.assertEqual(by_stage["agent-skills"]["expected_sha"], doc_shas[".github/workflows/documentation-freshness.yml"])
        self.assertEqual(by_stage["agent-skills"]["expected_shas"], doc_shas)
        self.assertEqual(by_stage["dependabot"]["expected_sha"], dep_sha)
        self.assertEqual(by_stage["dependabot"]["cadence"], "weekly")

        doc_policy = profile_to_policy(
            "minimal-lean",
            repository="acme/project",
            inventory=inv,
            options={"documentation_freshness": True},
        )
        doc_stages = {s["name"]: s for s in doc_policy["stages"]}
        self.assertEqual(doc_stages["documentation-freshness"]["expected_sha"], doc_shas[".github/workflows/documentation-freshness.yml"])
        self.assertEqual(doc_stages["documentation-freshness"]["expected_shas"], doc_shas)
        self.assertEqual(doc_stages["dependabot"]["expected_sha"], dep_sha)


if __name__ == "__main__":
    unittest.main()
