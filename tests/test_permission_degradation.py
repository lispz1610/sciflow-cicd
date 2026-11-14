import io
import json
from pathlib import Path
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from configure_github_repo import ApiError
from interactive import format_inventory_summary
from interactive_apply import format_apply_summary, format_dry_run_summary
from setup_github_repo import inventory, run_policy


class CleanBaseApi:
    def __init__(self, visibility="public", plan="free"):
        self.visibility = visibility
        self.plan = plan
        self.writes = []
        self.files = {}
        self.prs = []
        self.repo = {
            "name": "project",
            "full_name": "acme/project",
            "visibility": visibility,
            "default_branch": "main",
            "has_issues": True,
            "has_wiki": True,
            "has_projects": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "delete_branch_on_merge": True,
            "allow_update_branch": True,
            "owner": {"login": "acme", "type": "User"},
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            },
        }

    def request(self, method, path, payload=None):
        if method in ("PUT", "POST", "PATCH", "DELETE"):
            self.writes.append((method, path, payload))
            if method == "PATCH" and path == "/repos/acme/project" and isinstance(payload, dict):
                self.repo.update(payload)
            if method == "PUT" and "/contents/" in path:
                self.files[path] = payload or {}
                return 201, {"content": {"sha": "b" * 40}}
            if path == "/repos/acme/project/git/refs":
                return 201, {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
            if path.startswith("/repos/acme/project/pulls"):
                pr = {
                    "number": 12,
                    "html_url": "https://github.com/acme/project/pull/12",
                    "draft": True,
                    "head": {"ref": payload.get("head")},
                    "base": {"ref": payload.get("base")},
                }
                self.prs.append(pr)
                return 201, pr
            return 200, {}

        clean_path = path.split("?")[0]
        if clean_path in self.files:
            return 200, {
                "type": "file",
                "sha": "b" * 40,
                "encoding": "base64",
                "content": self.files[clean_path].get("content", ""),
            }

        if path == "/repos/acme/project":
            return 200, self.repo
        if path == "/users/acme":
            return 200, {"login": "acme", "plan": {"name": self.plan}}
        if path == "/repos/acme/project/git/ref/heads/main":
            return 200, {"object": {"sha": "1" * 40}}
        if path.startswith("/repos/acme/project/contents/"):
            return 404, None
        if path.startswith("/repos/acme/project/pulls"):
            return 200, list(self.prs)
        if path.startswith("/repos/acme/project/compare/"):
            return 200, {"files": [], "commits": []}
        if path == "/repos/acme/project/actions/permissions":
            return 200, {"enabled": True, "allowed_actions": "all"}
        if path == "/repos/acme/project/actions/permissions/workflow":
            return 200, {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
        if path == "/repos/acme/project/code-scanning/default-setup":
            return 200, {"state": "not-configured"}
        if path == "/repos/acme/project/vulnerability-alerts":
            return 204, None
        if path == "/repos/acme/project/automated-security-fixes":
            return 200, {"enabled": True}
        if path == "/repos/acme/project/private-vulnerability-reporting":
            return 200, {"enabled": False}
        if "/git/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            return 200, {"sha": sha}
        if path.startswith("/repos/acme/project/rulesets"):
            return 200, []

        return 404, None


class PermissionDegradationTests(unittest.TestCase):
    def test_inventory_permission_degradation_across_all_endpoints(self):
        class DeniedInventoryApi(CleanBaseApi):
            def request(self, method, path, payload=None):
                if any(endpoint in path for endpoint in (
                    "/actions/permissions",
                    "/code-scanning/default-setup",
                    "/vulnerability-alerts",
                    "/automated-security-fixes",
                    "/private-vulnerability-reporting",
                    "/rulesets",
                )):
                    raise ApiError(f"GET {path}: HTTP 403: Resource not accessible by personal access token")
                return super().request(method, path, payload)

        api = DeniedInventoryApi(visibility="private")
        inv = inventory(api, "acme/project")

        # Every degraded endpoint reports status "inaccessible" with explicit guidance
        self.assertEqual(inv["actions"]["status"], "inaccessible")
        self.assertIn("Settings > Actions > General", inv["actions"]["guidance"])

        self.assertEqual(inv["workflow_permissions"]["status"], "inaccessible")
        self.assertIn("Settings > Actions > General", inv["workflow_permissions"]["guidance"])

        self.assertEqual(inv["codeql"]["status"], "inaccessible")
        self.assertIn("Settings > Code security and analysis", inv["codeql"]["guidance"])

        self.assertEqual(inv["vulnerability_alerts"]["status"], "inaccessible")
        self.assertIn("Settings > Code security and analysis", inv["vulnerability_alerts"]["guidance"])

        self.assertEqual(inv["automated_security_fixes"]["status"], "inaccessible")
        self.assertIn("Settings > Code security and analysis", inv["automated_security_fixes"]["guidance"])

        self.assertEqual(inv["private_vulnerability_reporting"]["status"], "inaccessible")
        self.assertIn("Settings > Code security and analysis", inv["private_vulnerability_reporting"]["guidance"])

        self.assertEqual(inv["rulesets"]["status"], "inaccessible")
        self.assertIn("Settings > Rules > Rulesets", inv["rulesets"]["guidance"])

        # Dependency graph inherits inaccessible guidance when vulnerability alerts fail on private repo
        self.assertEqual(inv["dependency_graph"]["status"], "inaccessible")
        self.assertIn("Settings > Code security and analysis", inv["dependency_graph"]["guidance"])

        # Format summary produces human-readable output containing Settings paths
        summary = format_inventory_summary(inv)
        self.assertIn("Inaccessible (HTTP 403; configure in Settings > Actions > General)", summary)
        self.assertIn("Inaccessible (HTTP 403; configure in Settings > Code security and analysis)", summary)
        self.assertIn("Inaccessible (HTTP 403; configure in Settings > Rules > Rulesets)", summary)

    def test_dry_run_optional_capabilities_http_403_produces_clean_warnings_and_guidance(self):
        class DeniedPreflightApi(CleanBaseApi):
            def request(self, method, path, payload=None):
                if any(endpoint in path for endpoint in (
                    "/code-scanning/default-setup",
                    "/rulesets",
                    "/vulnerability-alerts",
                )):
                    raise ApiError(f"GET {path}: HTTP 403: Forbidden")
                return super().request(method, path, payload)

        api = DeniedPreflightApi(visibility="private")
        api.repo["has_issues"] = False
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "security", "enable": ["vulnerability-alerts"]},
                {"name": "codeql"},
                {"name": "features", "enable": ["issues"]},
            ],
        }
        result = run_policy(api, "acme/project", policy, apply=False)

        # Changes record blocked status with availability "inaccessible" and exact guidance
        blocked_stages = {c["stage"]: c for c in result["changes"] if c.get("status") == "blocked"}
        self.assertIn("security", blocked_stages)
        self.assertEqual(blocked_stages["security"]["availability"], "inaccessible")
        self.assertIn("Settings > Code security and analysis", blocked_stages["security"]["guidance"])

        self.assertIn("codeql", blocked_stages)
        self.assertEqual(blocked_stages["codeql"]["availability"], "inaccessible")
        self.assertIn("Settings > Code security and analysis > CodeQL analysis", blocked_stages["codeql"]["guidance"])

        # Warnings are produced with manual configuration action
        warnings = result.get("warnings", [])
        self.assertTrue(len(warnings) >= 2)
        self.assertTrue(any("security:" in w and "Settings > Code security and analysis" in w for w in warnings))
        self.assertTrue(any("codeql:" in w and "Settings > Code security and analysis" in w for w in warnings))

        # Dry run did not halt prematurely: features stage was also evaluated
        stages_evaluated = {c["stage"] for c in result["changes"]}
        self.assertIn("features", stages_evaluated)

        # Formatted dry run summary displays Guidance lines
        summary = format_dry_run_summary(result)
        self.assertIn("Guidance: Configure CodeQL manually in GitHub Settings > Code security and analysis", summary)
        self.assertIn("Warnings:", summary)

    def test_dry_run_unsupported_features_fail_with_actionable_guidance(self):
        # 1. Private repository rulesets requiring paid plan
        api = CleanBaseApi(visibility="private", plan="free")
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "gates", "evidence_pr": 10, "reviewer": "octocat",
                 "checks": {"ci": ".github/workflows/ci.yml"}},
            ],
        }
        result = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(result["changes"][0]["status"], "blocked")
        self.assertEqual(result["changes"][0]["availability"], "unsupported")
        self.assertIn("Pro, Team, or Enterprise plan", result["changes"][0]["guidance"])
        self.assertIn("Settings > Branches", result["changes"][0]["guidance"])

        # 2. Review setup on public repository
        api_public = CleanBaseApi(visibility="public")
        review_policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "review",
                    "workflow": {
                        "template": "open-code-review", "template_version": 1,
                        "filename": "open-code-review.yml", "triggers": ["pull_request_target"],
                        "events": ["opened", "synchronize", "reopened"], "fork_policy": "skip",
                        "action_sha": "182898cf522da3d04157b422752d028417974e19",
                        "ocr_version": "1.12.12", "max_tokens_budget": 50000,
                        "timeout_minutes": 30,
                    },
                    "llm": {
                        "url": "https://api.example.com/v1", "model": "review-model-v1",
                        "use_anthropic": False, "secret_name": "OCR_LLM_AUTH_TOKEN",
                    },
                    "limits": {"max_retention_days": 30},
                },
            ],
        }
        result_review = run_policy(api_public, "acme/project", review_policy, apply=False)
        self.assertEqual(result_review["changes"][0]["status"], "blocked")
        self.assertEqual(result_review["changes"][0]["availability"], "unsupported")
        self.assertIn("supports private repositories only", result_review["changes"][0]["guidance"])

        # 3. Private vulnerability reporting on private repository
        api_private = CleanBaseApi(visibility="private")
        sec_policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "security", "enable": ["private-vulnerability-reporting"]},
            ],
        }
        result_sec = run_policy(api_private, "acme/project", sec_policy, apply=False)
        self.assertEqual(result_sec["changes"][0]["status"], "blocked")
        self.assertEqual(result_sec["changes"][0]["availability"], "unsupported")
        self.assertIn("available only for public repositories", result_sec["changes"][0]["guidance"])

    def test_apply_safely_halts_when_blocked_by_permission_or_unsupported_capability(self):
        class DeniedApi(CleanBaseApi):
            def request(self, method, path, payload=None):
                if "/code-scanning/default-setup" in path:
                    raise ApiError(f"GET {path}: HTTP 403: Forbidden")
                return super().request(method, path, payload)

        api = DeniedApi(visibility="private")
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "codeql"},
                {"name": "features", "enable": ["issues"]},
            ],
        }
        # Apply mode
        result = run_policy(api, "acme/project", policy, apply=True)
        # Blocked change prevented any direct writes from occurring
        self.assertEqual(api.writes, [])
        self.assertTrue(any(c.get("status") == "blocked" for c in result["changes"]))

        # Apply summary formatting includes warnings and guidance
        summary = format_apply_summary(result)
        self.assertIn("Blocked: 1", summary)
        self.assertIn("Warnings and actionable guidance:", summary)

    def test_apply_succeeds_for_unblocked_settings_and_workflows_when_optional_capabilities_are_inaccessible(self):
        class DeniedOptionalApi(CleanBaseApi):
            def request(self, method, path, payload=None):
                if any(endpoint in path for endpoint in (
                    "/code-scanning/default-setup",
                    "/vulnerability-alerts",
                )):
                    raise ApiError(f"GET {path}: HTTP 403: Forbidden")
                return super().request(method, path, payload)

        api = DeniedOptionalApi(visibility="private")
        api.repo["has_issues"] = False
        api.repo["allow_merge_commit"] = True
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "security", "enable": ["vulnerability-alerts"]},
                {"name": "codeql"},
                {"name": "features", "enable": ["issues"]},
                {"name": "merge", "settings": {"allow_merge_commit": False}},
                {
                    "name": "workflows",
                    "workflows": [{
                        "template": "pr-ci",
                        "template_version": 1,
                        "filename": "ci.yml",
                        "triggers": ["pull_request"],
                        "checkout_sha": "a" * 40,
                        "commands": ["python3 -m unittest"],
                    }],
                },
            ],
        }

        # 1. Dry-run preflight
        dry_run = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertIn("warnings", dry_run)
        self.assertTrue(len(dry_run["warnings"]) >= 2)
        dry_summary = format_dry_run_summary(dry_run)
        self.assertIn("non-fatal degradation", dry_summary)
        self.assertIn("Optional capabilities are degraded; unblocked direct settings remain applicable.", dry_summary)

        # 2. Apply with accepted dry-run succeeds on unblocked settings and stages Setup PR
        apply_result = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(apply_result["mode"], "apply")

        # Direct writes occurred for features and merge
        written_paths = [w[1] for w in api.writes]
        self.assertIn("/repos/acme/project", written_paths)
        # Setup PR was staged for workflow files
        self.assertIn("setup_pull_request", apply_result)
        self.assertTrue(any(w[1].startswith("/repos/acme/project/git/refs") for w in api.writes))

        # Apply summary clearly distinguishes degraded capabilities
        apply_summary = format_apply_summary(apply_result)
        self.assertIn("non-fatal degraded capabilities: 2", apply_summary)
        self.assertIn("Verified direct settings are complete; optional capabilities degraded", apply_summary)

    def test_interactive_apply_applies_unblocked_settings_when_optional_capabilities_degraded(self):
        class MockSession:
            def __init__(self, api, repository, dry_run_path):
                self.api = api
                self.repository = repository
                self.dry_run_path = dry_run_path
                self.in_stream = io.StringIO("y\n")
                self.out_stream = io.StringIO()
                self.err_stream = io.StringIO()

        class DeniedCodeqlApi(CleanBaseApi):
            def request(self, method, path, payload=None):
                if "/code-scanning/default-setup" in path:
                    raise ApiError(f"GET {path}: HTTP 403: Forbidden")
                return super().request(method, path, payload)

        api = DeniedCodeqlApi(visibility="private")
        api.repo["has_issues"] = False
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "codeql"},
                {"name": "features", "enable": ["issues"]},
            ],
        }

        import tempfile
        from interactive_apply import run_interactive_apply
        with tempfile.TemporaryDirectory() as tmpdir:
            dry_run_path = Path(tmpdir) / "dry_run.json"
            session = MockSession(api, "acme/project", dry_run_path)
            exit_code = run_interactive_apply(session, policy)

            self.assertEqual(exit_code, 0)
            self.assertTrue(dry_run_path.exists())
            out = session.out_stream.getvalue()
            self.assertIn("non-fatal degradation", out)
            self.assertIn("Verified direct settings are complete; optional capabilities degraded", out)
            self.assertTrue(api.repo["has_issues"])


if __name__ == "__main__":
    unittest.main()
