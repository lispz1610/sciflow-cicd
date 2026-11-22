"""Contract tests for caller-authored content and metadata setup."""

import base64
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_repository_content.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("setup_repository_content", SCRIPT)
content = importlib.util.module_from_spec(spec)
spec.loader.exec_module(content)
import setup_github_repo as setup
from test_install_github_workflows import WorkflowApi


class ContentApi:
    def __init__(self):
        self.labels = {"unmanaged": {"name": "unmanaged", "color": "eeeeee", "description": "keep"}}
        self.files = {}
        self.comments = []
        self.writes = []

    def request(self, method, path, payload=None):
        base = "/repos/acme/project"
        if path == "/user":
            return 200, {"login": "operator"}
        if path == base:
            return 200, {"default_branch": "main", "visibility": "public"}
        if path.startswith(base + "/labels/"):
            name = path.removeprefix(base + "/labels/")
            if method == "GET":
                return (200, self.labels[name]) if name in self.labels else (404, None)
            self.writes.append((method, path, payload))
            self.labels[name].update(payload)
            return 200, self.labels[name]
        if path == base + "/labels" and method == "POST":
            self.writes.append((method, path, payload))
            self.labels[payload["name"]] = dict(payload)
            return 201, self.labels[payload["name"]]
        if path.startswith(base + "/contents/"):
            name = path.removeprefix(base + "/contents/").split("?", 1)[0]
            if method == "GET":
                if name not in self.files:
                    return 404, None
                return 200, dict(self.files[name], type="file", encoding="base64")
            self.writes.append((method, path, payload))
            self.files[name] = {"sha": f"{len(self.writes):040x}", "content": payload["content"]}
            return 201, {"content": self.files[name]}
        if path == base + "/issues/7":
            return 200, {"number": 7, "title": "Selected issue"}
        if path.startswith(base + "/issues/7/comments?per_page=100&page="):
            return 200, self.comments
        if path == base + "/issues/7/comments" and method == "POST":
            self.writes.append((method, path, payload))
            comment = {"id": len(self.comments) + 1, "body": payload["body"],
                       "user": {"login": "operator"}}
            self.comments.append(comment)
            return 201, comment
        if path.startswith(base + "/issues/comments/") and method == "PATCH":
            self.writes.append((method, path, payload))
            comment_id = int(path.rsplit("/", 1)[-1])
            comment = next(item for item in self.comments if item["id"] == comment_id)
            comment["body"] = payload["body"]
            return 200, comment
        raise AssertionError((method, path, payload))


class ContentTests(unittest.TestCase):
    def test_labels_create_and_update_only_selected_names(self):
        api = ContentApi()
        items = [{"name": "spec", "color": "123abc", "description": "Planning"}]
        content.reconcile_labels(api, "acme/project", items, apply=True)
        api.writes.clear()
        self.assertEqual(content.reconcile_labels(api, "acme/project", items, apply=True), [])
        self.assertEqual(api.labels["unmanaged"]["description"], "keep")
        items[0]["description"] = "Updated planning"
        update = content.reconcile_labels(api, "acme/project", items, apply=True)[0]
        self.assertEqual(update[0], "PATCH")
        self.assertEqual(update[2], {"description": "Updated planning"})

    def test_authored_file_is_idempotent_and_drift_is_blocked(self):
        api = ContentApi()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "form.yml"
            source.write_text("name: Feature\n", encoding="utf-8")
            item = {"path": ".github/ISSUE_TEMPLATE/feature.yml", "source": str(source)}
            content.reconcile_files(api, "acme/project", [item], apply=True)
            self.assertEqual(content.reconcile_files(api, "acme/project", [item], apply=True), [])
            self.assertEqual(base64.b64decode(api.files[item["path"]]["content"]), b"name: Feature\n")
            source.write_text("name: Changed\n", encoding="utf-8")
            with self.assertRaisesRegex(content.ApiError, "drift"):
                content.reconcile_files(api, "acme/project", [item], apply=True)
            item["expected_sha"] = api.files[item["path"]]["sha"]
            self.assertEqual(content.reconcile_files(api, "acme/project", [item], apply=True)[0][0], "PUT")

    def test_invalid_destination_is_rejected_without_writes(self):
        api = ContentApi()
        with self.assertRaises(ValueError):
            content.reconcile_files(api, "acme/project", [{"path": "../outside", "source": "x"}], apply=True)
        self.assertEqual(api.writes, [])

    def test_overlong_label_description_is_rejected_before_write(self):
        api = ContentApi()
        with self.assertRaisesRegex(ValueError, "description"):
            content.reconcile_labels(api, "acme/project", [{
                "name": "spec", "color": "123abc", "description": "x" * 101}], apply=True)
        self.assertEqual(api.writes, [])

    def test_links_update_only_owned_comment_and_do_not_duplicate(self):
        api = ContentApi()
        api.comments.append({"id": 1, "body": "Leave me alone", "user": {"login": "other"}})
        entry = {"issue": 7, "key": "planning", "links": [
            {"label": "Spec", "url": "https://github.com/acme/project/wiki/Spec-7"}]}
        content.reconcile_links(api, "acme/project", [entry], apply=True)
        api.writes.clear()
        self.assertEqual(content.reconcile_links(api, "acme/project", [entry], apply=True), [])
        self.assertEqual(api.comments[0]["body"], "Leave me alone")
        entry["links"][0]["url"] = "https://github.com/acme/project/wiki/Spec-7/revision"
        self.assertEqual(content.reconcile_links(api, "acme/project", [entry], apply=True)[0][0], "PATCH")

    def test_policy_preflights_content_stages_and_redacts_file_bytes(self):
        api = WorkflowApi()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "form.yml"
            source.write_text("name: Feature\n", encoding="utf-8")
            policy = {"version": 1, "stages": [
                {"name": "files", "items": [{"path": ".github/ISSUE_TEMPLATE/feature.yml", "source": str(source)}]},
            ]}
            plan = setup.run_policy(api, "acme/project", policy)
            self.assertEqual(len(plan["changes"]), 1)
            self.assertNotIn("name: Feature", str(plan))
            self.assertEqual(api.writes, [])
            applied = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=plan)
            self.assertEqual(applied["changes"][0]["status"], "pending")
            self.assertEqual(len(api.prs), 1)
            api.writes.clear()
            plan2 = setup.run_policy(api, "acme/project", policy)
            self.assertEqual(setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=plan2)["changes"], [])
            self.assertEqual(api.writes, [])

    def test_invalid_last_content_stage_prevents_earlier_writes(self):
        api = ContentApi()
        policy = {"version": 1, "stages": [
            {"name": "labels", "items": [{"name": "spec", "color": "123abc", "description": "Planning"}]},
            {"name": "files", "items": [{"path": "../invalid", "source": "missing"}]},
        ]}
        with self.assertRaises(ValueError):
            setup.run_policy(api, "acme/project", policy, apply=True)
        self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()
