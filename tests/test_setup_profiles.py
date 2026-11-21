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

        # Features: issues enabled, wiki disabled (not present)
        features_stage = next(s for s in baseline if s["name"] == "features")
        self.assertIn("issues", features_stage["enable"])
        self.assertNotIn("wiki", features_stage["enable"])

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
        self.assertIn("ci-profile", stage_names)
        self.assertIn("codeql", stage_names)

        ci_stage = next(s for s in policy["stages"] if s["name"] == "ci-profile")
        self.assertEqual(ci_stage["profile"], "python")
        self.assertTrue(ci_stage["python"]["cache"])
        self.assertTrue(ci_stage["python"]["lfs"])

    def test_hpc_cmake_mpi_policy_structure(self):
        policy = profile_to_policy("hpc-cmake-mpi", repository="octocat/hello-world")
        stage_names = {s["name"] for s in policy["stages"]}
        self.assertIn("ci-profile", stage_names)
        self.assertIn("codeql", stage_names)

        ci_stage = next(s for s in policy["stages"] if s["name"] == "ci-profile")
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


if __name__ == "__main__":
    unittest.main()
