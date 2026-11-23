"""Structural checks for the continuous automation workflows."""

from pathlib import Path
import re
import tempfile
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML
    yaml = None


ROOT = Path(__file__).resolve().parents[1]


from scripts.issue_triage import GitHubApiError, _request, apply_plan, plan_event
from scripts.lint_workflow_action_pins import audit_workflows


class WorkflowActionPinAuditTests(unittest.TestCase):
    def test_all_repository_workflows_use_local_or_full_sha_references(self):
        self.assertEqual(audit_workflows(ROOT / ".github" / "workflows"), [])

    def test_accepts_full_sha_and_local_action_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            workflow_dir = Path(temporary) / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "valid.yml").write_text(
                "permissions:\n"
                "  contents: read\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "      - name: Pinned action\n"
                "        uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n"
                "      - name: Local action\n"
                "        uses: ./.github/actions/build\n",
                encoding="utf-8",
            )

            findings = audit_workflows(workflow_dir)

        self.assertEqual(findings, [])

    def test_reports_file_location_and_floating_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            workflow_dir = Path(temporary) / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "unsafe.yml").write_text(
                "permissions:\n"
                "  contents: read\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "      - name: Checkout code\n"
                "        uses: actions/checkout@v5\n",
                encoding="utf-8",
            )

            findings = audit_workflows(workflow_dir)

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertIn("unsafe.yml", finding)
        self.assertIn("Checkout code", finding)
        self.assertIn("actions/checkout@v5", finding)

    def test_audits_yaml_workflow_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            workflow_dir = Path(temporary) / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "unsafe.yaml").write_text(
                "permissions:\n"
                "  contents: read\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "      - name: Checkout code\n"
                "        uses: actions/checkout@v5\n",
                encoding="utf-8",
            )

            findings = audit_workflows(workflow_dir)

        self.assertEqual(len(findings), 1)
        self.assertIn("unsafe.yaml", findings[0])
        self.assertIn("actions/checkout@v5", findings[0])

    def test_treats_unparseable_workflow_as_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            workflow_dir = Path(temporary) / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "broken.yml").write_text("jobs: [\n", encoding="utf-8")

            findings = audit_workflows(workflow_dir)

        self.assertEqual(len(findings), 1)
        self.assertIn("broken.yml", findings[0])
        self.assertIn("could not parse workflow", findings[0])

    @unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
    def test_security_audit_runs_for_workflow_changes_with_read_only_permissions(self):
        path = ROOT / ".github" / "workflows" / "workflow-security-audit.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)

        self.assertEqual(workflow["on"]["pull_request"]["paths"], [".github/workflows/**"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["audit-workflow-actions"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertIn("scripts/lint_workflow_action_pins.py", job["steps"][-1]["run"])
        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")



class IssueTriagePlanTests(unittest.TestCase):
    def setUp(self):
        self.issue = {
            "number": 42,
            "node_id": "I_issue42",
            "user": {"login": "reporter"},
            "labels": [],
        }

    def test_opened_issue_gets_triage_label_without_replacing_existing_labels(self):
        self.issue["labels"] = [{"name": "bug"}]

        plan = plan_event("issues", {"action": "opened", "issue": self.issue})

        self.assertEqual(plan["labels_to_add"], ["needs-triage"])
        self.assertEqual(plan["labels_to_remove"], [])

    def test_opened_issue_with_canonical_triage_label_keeps_its_triage_state(self):
        for label in ("needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix"):
            with self.subTest(label=label):
                self.issue["labels"] = [{"name": label}, {"name": "bug"}]

                plan = plan_event("issues", {"action": "opened", "issue": self.issue})

                self.assertEqual(plan["labels_to_add"], [])

    def test_issue_author_comment_moves_needs_info_back_to_triage(self):
        self.issue["labels"] = [{"name": "needs-info"}, {"name": "bug"}]
        payload = {
            "action": "created",
            "issue": self.issue,
            "comment": {"user": {"login": "reporter"}},
        }

        plan = plan_event("issue_comment", payload)

        self.assertEqual(plan["labels_to_add"], ["needs-triage"])
        self.assertEqual(plan["labels_to_remove"], ["needs-info"])

    def test_non_author_comment_does_not_change_labels(self):
        self.issue["labels"] = [{"name": "needs-info"}]
        payload = {
            "action": "created",
            "issue": self.issue,
            "comment": {"user": {"login": "maintainer"}},
        }

        plan = plan_event("issue_comment", payload)

        self.assertEqual(plan["labels_to_add"], [])
        self.assertEqual(plan["labels_to_remove"], [])

    def test_project_item_is_planned_only_for_new_issues_with_configured_project(self):
        opened = {"action": "opened", "issue": self.issue}
        commented = {
            "action": "created",
            "issue": self.issue,
            "comment": {"user": {"login": "reporter"}},
        }

        self.assertEqual(plan_event("issues", opened, "P_board")["project_item_node_id"], "I_issue42")
        self.assertIsNone(plan_event("issues", opened)["project_item_node_id"])
        self.assertIsNone(plan_event("issue_comment", commented, "P_board")["project_item_node_id"])

    def test_author_identity_rule_applies_even_when_the_issue_author_is_a_bot(self):
        self.issue["user"]["login"] = "automation[bot]"
        self.issue["labels"] = [{"name": "needs-info"}]
        payload = {
            "action": "created",
            "issue": self.issue,
            "comment": {"user": {"login": "automation[bot]"}},
        }

        plan = plan_event("issue_comment", payload)

        self.assertEqual(plan["labels_to_remove"], ["needs-info"])

    def test_project_mutation_uses_a_separate_project_credential(self):
        from unittest.mock import patch

        plan = {
            "labels_to_add": ["needs-triage"],
            "labels_to_remove": [],
            "project_item_node_id": "I_issue42",
        }
        with patch("scripts.issue_triage._request") as request:
            request.return_value = {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_42"}}}}
            apply_plan("octo/widget", 42, plan, "issue-token", "https://api.github.com", project_token="project-token", project_id="P_board")
        self.assertEqual(request.call_args_list[0].args[1], "issue-token")
        self.assertEqual(request.call_args_list[1].args[1], "project-token")

    def test_project_mutation_fails_without_project_credential(self):
        plan = {"labels_to_add": [], "labels_to_remove": [], "project_item_node_id": "I_issue42"}
        with self.assertRaisesRegex(GitHubApiError, "TRIAGE_PROJECT_TOKEN"):
            apply_plan("octo/widget", 42, plan, "", "https://api.github.com", project_id="P_board")

    def test_project_mutation_rejects_missing_result_item(self):
        from unittest.mock import patch

        plan = {"labels_to_add": [], "labels_to_remove": [], "project_item_node_id": "I_issue42"}
        with patch("scripts.issue_triage._request", return_value={"data": {"addProjectV2ItemById": None}}):
            with self.assertRaisesRegex(GitHubApiError, "project item"):
                apply_plan("octo/widget", 42, plan, "", "https://api.github.com", project_token="project-token", project_id="P_board")

        with patch("scripts.issue_triage._request", return_value={"data": []}):
            with self.assertRaisesRegex(GitHubApiError, "project item"):
                apply_plan("octo/widget", 42, plan, "", "https://api.github.com", project_token="project-token", project_id="P_board")

    def test_label_api_list_response_is_accepted(self):
        from unittest.mock import patch

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'[{"name":"needs-triage"}]'

        with patch("scripts.issue_triage.urlopen", return_value=Response()):
            self.assertEqual(
                _request("https://api.github.com/repos/octo/widget/issues/42/labels", "issue-token", {"labels": ["needs-triage"]}),
                [{"name": "needs-triage"}],
            )


@unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
class IssueTriageWorkflowTests(unittest.TestCase):
    def test_issue_triage_workflow_uses_minimal_permissions_and_pinned_actions(self):
        path = ROOT / ".github" / "workflows" / "issue-triage.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)

        self.assertEqual(workflow["on"]["issues"]["types"], ["opened"])
        self.assertEqual(workflow["on"]["issue_comment"]["types"], ["created"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        label_job = workflow["jobs"]["label-triage"]
        self.assertEqual(label_job["permissions"], {"contents": "read", "issues": "write"})
        steps = label_job["steps"]
        for step in steps:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        triage = next(step for step in steps if step.get("name") == "Apply issue labels")
        self.assertIn("--mode labels", triage["run"])
        self.assertNotIn("TRIAGE_PROJECT_TOKEN", triage["env"])
        self.assertIn("scripts/issue_triage.py", triage["run"])
        project_job = workflow["jobs"]["add-to-project"]
        self.assertIn("github.event_name == 'issues'", project_job["if"])
        self.assertIn("vars.TRIAGE_PROJECT_ID != ''", project_job["if"])
        self.assertEqual(project_job["permissions"], {"contents": "read"})
        project = next(step for step in project_job["steps"] if step.get("name") == "Add issue to project")
        self.assertIn("--mode project", project["run"])
        self.assertEqual(project["env"]["TRIAGE_PROJECT_ID"], "${{ vars.TRIAGE_PROJECT_ID }}")
        self.assertEqual(project["env"]["TRIAGE_PROJECT_TOKEN"], "${{ secrets.TRIAGE_PROJECT_TOKEN }}")
        self.assertNotIn("GITHUB_TOKEN", project["env"])


@unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
class WikiSyncWorkflowTests(unittest.TestCase):
    def test_wiki_sync_workflow_has_safe_push_trigger_and_pinned_permissions(self):
        path = ROOT / ".github" / "workflows" / "wiki-sync.yml"
        workflow_text = path.read_text()
        workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)

        self.assertEqual(workflow["on"]["push"]["branches"], ["main"])
        self.assertEqual(workflow["on"]["push"]["paths"], ["docs/**", "README.md"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["jobs"]["publish-wiki"]["runs-on"], "ubuntu-24.04")
        self.assertEqual(workflow["jobs"]["publish-wiki"]["permissions"], {"contents": "write"})

        steps = workflow["jobs"]["publish-wiki"]["steps"]
        uses_values = [step["uses"] for step in steps if "uses" in step]
        self.assertTrue(uses_values)
        for uses in uses_values:
            self.assertRegex(uses, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")

        publish = next(step for step in steps if step.get("name") == "Publish Wiki pages")
        self.assertEqual(publish["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertIn("scripts/publish_github_wiki.py", publish["run"])
        self.assertIn("--apply", publish["run"])
        self.assertNotRegex(publish["run"], r"https://[^\s]*:[^\s]*@")


@unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
class PolicyDriftWorkflowTests(unittest.TestCase):
    def test_policy_drift_workflow_runs_scheduled_and_manual_read_only_audit(self):
        path = ROOT / ".github" / "workflows" / "policy-drift-audit.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)

        self.assertEqual(workflow["on"]["schedule"][0]["cron"], "17 8 * * 1")
        self.assertEqual(workflow["on"]["workflow_dispatch"], "")
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["audit-policy"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertEqual(
            job["outputs"]["drift_report"],
            "${{ steps.compare.outputs.drift_report }}",
        )
        compare = next(step for step in job["steps"] if "Compare repository" in step.get("name", ""))
        self.assertEqual(compare["id"], "compare")
        self.assertIn("scripts/configure_github_repo.py", compare["run"])
        self.assertIn("--policy .github/repository-policy.json", compare["run"])
        self.assertIn("--token-env POLICY_AUDIT_TOKEN", compare["run"])
        self.assertIn("base64.b64encode", compare["run"])
        self.assertIn("GITHUB_OUTPUT", compare["run"])
        self.assertEqual(compare["env"]["POLICY_AUDIT_TOKEN"], "${{ secrets.POLICY_AUDIT_TOKEN }}")
        report_job = workflow["jobs"]["report-drift"]
        self.assertEqual(report_job["needs"], "audit-policy")
        self.assertEqual(report_job["runs-on"], "ubuntu-24.04")
        self.assertEqual(
            report_job["permissions"],
            {"contents": "read", "issues": "write"},
        )
        report = next(
            step for step in report_job["steps"] if "Report drift" in step.get("name", "")
        )
        self.assertIn("scripts/policy_drift_audit.py", report["run"])
        self.assertEqual(report["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertEqual(
            report["env"]["POLICY_DRIFT_REPORT"],
            "${{ needs.audit-policy.outputs.drift_report }}",
        )
        self.assertIn("base64 --decode", report["run"])


@unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
class DocsLinksAuditWorkflowTests(unittest.TestCase):
    def test_docs_link_audit_triggers_with_minimal_permissions_and_main_only_reconciliation(self):
        path = ROOT / ".github" / "workflows" / "docs-links-audit.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)

        self.assertEqual(
            workflow["on"]["pull_request"]["paths"],
            ["README.md", "*.md", "**/*.md", "docs/**"],
        )
        self.assertEqual(workflow["on"]["push"]["branches"], ["main"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})

        job = workflow["jobs"]["audit-documentation"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(job["permissions"], {"contents": "read"})
        steps = job["steps"]
        checkout = next(step for step in steps if step.get("name") == "Check out repository")
        self.assertRegex(checkout["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        link_check = next(step for step in steps if step.get("name") == "Check relative Markdown links")
        self.assertIn("scripts/check_markdown_links.py", link_check["run"])

        reconcile_job = workflow["jobs"]["reconcile-managed-links"]
        self.assertEqual(
            reconcile_job["if"],
            "github.event_name == 'push' && github.ref == 'refs/heads/main'",
        )
        self.assertEqual(
            reconcile_job["permissions"],
            {"contents": "read", "issues": "write"},
        )
        reconcile = next(
            step for step in reconcile_job["steps"]
            if step.get("name") == "Reconcile configured GitHub issue links"
        )
        self.assertEqual(reconcile["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertIn("scripts/reconcile_github_links.py", reconcile["run"])
        self.assertIn(".github/managed-links.json", reconcile["run"])
        self.assertIn("--apply", reconcile["run"])
        self.assertNotIn("examples/links.json", reconcile["run"])


@unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
class ReleasePromotionWorkflowStructureTests(unittest.TestCase):
    def test_release_promotion_workflow_includes_sbom_generation_and_release_attachment(self):
        path = ROOT / ".github" / "workflows" / "release-promotion.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)

        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["promote-release"]
        self.assertEqual(job["permissions"], {"contents": "write", "checks": "read", "pull-requests": "read"})
        self.assertIn("pull_request.merged == true", job["if"])
        self.assertIn("release-candidate", job["if"])

        steps = job["steps"]
        prepare_step = next(step for step in steps if step.get("name") == "Validate release evidence and prepare assets")
        self.assertIn("scripts/release_promotion.py", prepare_step["run"])
        self.assertEqual(prepare_step["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")

        # Redundant separate Export release SBOM step should not exist
        sbom_steps = [step for step in steps if "Export release SBOM" in step.get("name", "")]
        self.assertEqual(len(sbom_steps), 0, "Release promotion must not redundantly recreate sbom.spdx.json")

        release_step = next(step for step in steps if "Publish GitHub Release" in step.get("name", ""))
        self.assertIn("OPTIONAL_ASSETS", release_step["run"])
        self.assertIn("audit-report.md", release_step["run"])
        self.assertIn("runtime-fingerprint.txt", release_step["run"])


@unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
class DependencyReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow_path = ROOT / ".github" / "workflows" / "dependency-review.yml"

    def _load_workflow(self):
        self.assertTrue(
            self.workflow_path.is_file(),
            f"Expected workflow file at {self.workflow_path} to exist",
        )
        return yaml.load(self.workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_dependency_review_workflow_exists_and_parses(self):
        workflow = self._load_workflow()
        self.assertEqual(workflow.get("name"), "Dependency review")

    def test_dependency_review_workflow_triggers_and_path_filters(self):
        workflow = self._load_workflow()
        self.assertIn("on", workflow)
        self.assertIn("pull_request", workflow["on"])
        pr_trigger = workflow["on"]["pull_request"]

        self.assertEqual(pr_trigger.get("branches"), ["main"])
        expected_types = ["opened", "synchronize", "reopened"]
        self.assertEqual(pr_trigger.get("types"), expected_types)

        expected_paths = [
            "requirements.txt",
            "pyproject.toml",
            "**/requirements*.txt",
            "setup.py",
        ]
        self.assertEqual(pr_trigger.get("paths"), expected_paths)

    def test_dependency_review_workflow_permissions(self):
        workflow = self._load_workflow()
        expected_perms = {"contents": "read", "pull-requests": "write"}
        self.assertEqual(workflow.get("permissions"), expected_perms)

        jobs = workflow.get("jobs", {})
        self.assertTrue(jobs, "Workflow must define at least one job")
        for job_name, job in jobs.items():
            if "permissions" in job:
                self.assertEqual(job["permissions"], expected_perms)

    def test_dependency_review_workflow_uses_pinned_actions(self):
        workflow = self._load_workflow()
        jobs = workflow.get("jobs", {})
        self.assertTrue(jobs)

        sha_regex = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        action_references = []
        for job in jobs.values():
            for step in job.get("steps", []):
                if "uses" in step:
                    action_references.append(step["uses"])
                    self.assertRegex(
                        step["uses"],
                        sha_regex,
                        f"Action {step['uses']} must be pinned with a 40-character SHA",
                    )

        dep_review_action = "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294"
        self.assertIn(dep_review_action, action_references)

    def test_dependency_review_workflow_inputs_and_license_policy(self):
        workflow = self._load_workflow()
        jobs = workflow.get("jobs", {})
        review_step = None
        for job in jobs.values():
            for step in job.get("steps", []):
                if "actions/dependency-review-action" in step.get("uses", ""):
                    review_step = step
                    break
            if review_step:
                break

        self.assertIsNotNone(review_step, "Could not find actions/dependency-review-action step")
        step_with = review_step.get("with", {})
        self.assertEqual(step_with.get("fail-on-severity"), "high")

        expected_allow_licenses = "MIT, Apache-2.0, BSD-3-Clause, BSD-2-Clause, ISC, Python-2.0"
        self.assertEqual(step_with.get("allow-licenses"), expected_allow_licenses)
        self.assertNotIn("deny-licenses", step_with)


@unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
class IssueFrontierOrchestratorWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow_path = ROOT / ".github" / "workflows" / "issue-frontier-orchestrator.yml"

    def _load_workflow(self):
        self.assertTrue(
            self.workflow_path.is_file(),
            f"Expected workflow file at {self.workflow_path} to exist",
        )
        return yaml.load(self.workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_workflow_exists_and_parses(self):
        workflow = self._load_workflow()
        self.assertEqual(workflow.get("name"), "Issue frontier orchestrator")

    def test_workflow_triggers(self):
        workflow = self._load_workflow()
        self.assertIn("on", workflow)
        self.assertIn("issues", workflow["on"])
        self.assertEqual(workflow["on"]["issues"].get("types"), ["closed"])

    def test_workflow_permissions(self):
        workflow = self._load_workflow()
        expected_perms = {"contents": "read", "issues": "write"}
        self.assertEqual(workflow.get("permissions"), expected_perms)

        jobs = workflow.get("jobs", {})
        self.assertTrue(jobs, "Workflow must define at least one job")
        for job in jobs.values():
            if "permissions" in job:
                self.assertEqual(job["permissions"], expected_perms)

    def test_workflow_uses_pinned_actions(self):
        workflow = self._load_workflow()
        jobs = workflow.get("jobs", {})
        self.assertTrue(jobs)

        sha_regex = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        action_references = []
        for job in jobs.values():
            for step in job.get("steps", []):
                if "uses" in step:
                    action_references.append(step["uses"])
                    self.assertRegex(
                        step["uses"],
                        sha_regex,
                        f"Action {step['uses']} must be pinned with a 40-character SHA",
                    )

        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", action_references)
        self.assertIn("actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0", action_references)

    def test_workflow_runs_orchestration_script(self):
        workflow = self._load_workflow()
        jobs = workflow.get("jobs", {})
        run_steps = []
        for job in jobs.values():
            for step in job.get("steps", []):
                if "run" in step:
                    run_steps.append(step["run"])

        expected_command = 'python3 scripts/orchestrate_issue_frontier.py --event "$EVENT_PATH"'
        self.assertTrue(
            any(expected_command in cmd for cmd in run_steps),
            f"Expected command '{expected_command}' not found in job steps: {run_steps}",
        )

    def test_workflow_and_script_have_code_owners(self):
        owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        self.assertIn("/scripts/orchestrate_issue_frontier.py @lispz1610", owners)
        self.assertIn("/.github/workflows/issue-frontier-orchestrator.yml @lispz1610", owners)


@unittest.skipIf(yaml is None, "PyYAML is required to validate GitHub Actions workflow structure")
class PythonAppWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow_path = ROOT / ".github" / "workflows" / "python-app.yml"

    def _load_workflow(self):
        self.assertTrue(
            self.workflow_path.is_file(),
            f"Expected workflow file at {self.workflow_path} to exist",
        )
        return yaml.load(self.workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_workflow_exists_and_parses(self):
        workflow = self._load_workflow()
        self.assertEqual(workflow.get("name"), "Python application")

    def test_build_job_contains_module_dependency_audit_step(self):
        workflow = self._load_workflow()
        build_job = workflow.get("jobs", {}).get("build", {})
        steps = build_job.get("steps", [])
        audit_step = None
        for step in steps:
            if step.get("name") == "Audit architectural module dependencies":
                audit_step = step
                break
        self.assertIsNotNone(audit_step, "Could not find step 'Audit architectural module dependencies' in build job")
        self.assertIn("python3 scripts/audit_module_dependencies.py", audit_step.get("run", ""))

    def test_workflow_uses_pinned_actions(self):
        workflow = self._load_workflow()
        jobs = workflow.get("jobs", {})
        self.assertTrue(jobs)

        sha_regex = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        for job_name, job in jobs.items():
            for step in job.get("steps", []):
                if "uses" in step:
                    self.assertRegex(
                        step["uses"],
                        sha_regex,
                        f"Action {step['uses']} in job {job_name} must be pinned with a 40-character SHA",
                    )

    def test_build_matrix_runs_only_on_push(self):
        """The push-only build owns lint/audit/pytest; PRs must not repeat its matrix."""
        workflow = self._load_workflow()
        build_job = workflow.get("jobs", {}).get("build", {})
        self.assertEqual(build_job.get("if"), "github.event_name == 'push'")
        self.assertEqual(
            build_job.get("strategy", {}).get("matrix", {}).get("python-version"),
            ["3.10", "3.11", "3.12"],
        )
        run_steps = [step.get("run", "") for step in build_job.get("steps", [])]
        self.assertTrue(any("audit_module_dependencies.py" in step for step in run_steps))
        self.assertTrue(any("pytest" in step for step in run_steps))

    def test_pull_request_python_matrix_is_owned_by_ci_workflow(self):
        """ci.yml remains the single PR Python matrix with the stable status check."""
        ci_path = ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(ci_path.is_file())
        ci_workflow = yaml.load(ci_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        triggers = ci_workflow.get("on") if "on" in ci_workflow else ci_workflow.get(True)
        self.assertIn("pull_request", triggers)
        ci_job = ci_workflow.get("jobs", {}).get("ci", {})
        self.assertEqual(
            ci_job.get("strategy", {}).get("matrix", {}).get("python-version"),
            ["3.10", "3.11", "3.12"],
        )
        self.assertTrue(any("pytest" in step.get("run", "")
                            for step in ci_job.get("steps", [])))
        self.assertIn("status-check", ci_workflow.get("jobs", {}))

    def test_verify_release_survives_skipped_push_only_build(self):
        """verify-release still runs on PRs even though the build matrix is push-only."""
        workflow = self._load_workflow()
        verify_job = workflow.get("jobs", {}).get("verify-release", {})
        self.assertEqual(verify_job.get("needs"), "build")
        condition = verify_job.get("if", "")
        self.assertIn("always()", condition)
        self.assertIn("needs.build.result == 'skipped'", condition)
        self.assertEqual(
            verify_job.get("permissions"),
            {"contents": "read", "checks": "read", "pull-requests": "read"},
        )

    def test_audit_module_dependencies_has_code_owner(self):
        owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        self.assertIn("/scripts/audit_module_dependencies.py @lispz1610", owners)


if __name__ == "__main__":
    unittest.main()

