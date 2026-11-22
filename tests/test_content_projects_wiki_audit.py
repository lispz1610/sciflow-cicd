"""Comprehensive audit of managed content, Projects V2, and Wiki contracts (Ticket #17)."""

import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_setup_repository_content import ContentApi, content
from test_publish_github_wiki import git, publisher
from test_setup_github_projects import FakeGraphQL, projects


class ContentProjectsWikiAuditTests(unittest.TestCase):
    def test_labels_modify_only_selected_names_and_preserve_unmanaged(self):
        """Labels stage creates/updates only selected labels, preserving unmanaged labels."""
        api = ContentApi()
        # Seed an unmanaged label in repository
        api.labels["existing-label"] = {
            "name": "existing-label",
            "color": "000000",
            "description": "Do not touch",
        }

        # Select only 'bug-report'
        selected = [{"name": "bug-report", "color": "d73a4a", "description": "Bug fix"}]
        applied = content.reconcile_labels(api, "acme/project", selected, apply=True)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0][0], "POST")
        self.assertEqual(api.labels["bug-report"]["description"], "Bug fix")
        # Unmanaged label remains untouched
        self.assertIn("existing-label", api.labels)
        self.assertEqual(api.labels["existing-label"]["description"], "Do not touch")

        # Convergent re-application produces zero writes
        repeat = content.reconcile_labels(api, "acme/project", selected, apply=True)
        self.assertEqual(repeat, [])

    def test_authored_files_cas_drift_detection(self):
        """Authored files update requires expected_sha when repository content has drifted."""
        api = ContentApi()
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "feature.yml"
            src.write_text("name: Feature Form\n", encoding="utf-8")
            item = {"path": ".github/ISSUE_TEMPLATE/feature.yml", "source": str(src)}

            # First apply creates the file
            content.reconcile_files(api, "acme/project", [item], apply=True)
            self.assertIn(item["path"], api.files)

            # Change local content without expected_sha -> drift error!
            src.write_text("name: Changed Form\n", encoding="utf-8")
            with self.assertRaisesRegex(content.ApiError, "drift"):
                content.reconcile_files(api, "acme/project", [item], apply=True)

            # Supplying correct expected_sha allows compare-and-swap
            item["expected_sha"] = api.files[item["path"]]["sha"]
            updated = content.reconcile_files(api, "acme/project", [item], apply=True)
            self.assertEqual(len(updated), 1)
            self.assertEqual(updated[0][0], "PUT")

    def test_reconcile_files_rejects_malformed_expected_sha(self):
        """reconcile_files enforces 40-char hex SHA and handles expected_sha=None on absent files."""
        api = ContentApi()
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "feature.yml"
            src.write_text("name: Feature Form\n", encoding="utf-8")

            bad_item = {
                "path": ".github/ISSUE_TEMPLATE/feature.yml",
                "source": str(src),
                "expected_sha": "not-a-40-char-sha",
            }
            with self.assertRaisesRegex(ValueError, "expected_sha must be a 40-character blob SHA"):
                content.reconcile_files(api, "acme/project", [bad_item], apply=False)

            none_item = {
                "path": ".github/ISSUE_TEMPLATE/feature.yml",
                "source": str(src),
                "expected_sha": None,
            }
            planned = content.reconcile_files(api, "acme/project", [none_item], apply=False)
            self.assertEqual(len(planned), 1)

            absent_cas_item = {
                "path": ".github/ISSUE_TEMPLATE/feature.yml",
                "source": str(src),
                "expected_sha": "b" * 40,
            }
            with self.assertRaisesRegex(content.ApiError, "expected file is absent"):
                content.reconcile_files(api, "acme/project", [absent_cas_item], apply=False)

    def test_managed_link_comments_idempotence(self):
        """Managed link comments update only owned comments and do not duplicate."""
        api = ContentApi()
        links = [
            {
                "issue": 7,
                "key": "spec",
                "links": [{"label": "Spec 123", "url": "https://example.com/spec"}],
            }
        ]
        # First reconciliation creates comment
        content.reconcile_links(api, "acme/project", links, apply=True)
        self.assertEqual(len(api.comments), 1)
        comment_id = api.comments[0]["id"]

        # Second reconciliation with same links does not create duplicate
        content.reconcile_links(api, "acme/project", links, apply=True)
        self.assertEqual(len(api.comments), 1)
        self.assertEqual(api.comments[0]["id"], comment_id)

    def test_projects_v2_reconcile_idempotence(self):
        """Projects V2 reconciles fields and items without duplicate creation."""
        client = FakeGraphQL()
        policy = {
            "version": 1,
            "owner_type": "user",
            "owner": "testuser",
            "title": "Roadmap",
            "fields": [
                {
                    "name": "Status",
                    "type": "SINGLE_SELECT",
                    "options": [
                        {"name": "Todo", "color": "GRAY", "description": "Pending work"},
                        {"name": "Done", "color": "GREEN", "description": "Completed work"},
                    ],
                }
            ],
            "items": [{"content_id": "I1", "type": "Issue", "values": {"Status": "Todo"}}],
        }
        res1 = projects.reconcile(client, policy, apply=True)
        project_id = res1["project_id"]
        self.assertTrue(project_id.startswith("P"))

        # Reapplying with pinned project_id produces no duplicate project
        policy["project_id"] = project_id
        res2 = projects.reconcile(client, policy, apply=True)
        self.assertEqual(res2["project_id"], project_id)
        self.assertEqual(len(client.projects), 1)

    def test_wiki_publishing_preserves_unmanaged_pages(self):
        """Wiki publishing updates only selected pages and preserves unmanaged pages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            remote = root / "wiki.git"
            git("init", "--bare", "-b", "master", str(remote))
            seed = root / "seed"
            git("clone", str(remote), str(seed))
            (seed / "Home.md").write_text("# Home\n")
            (seed / "Unmanaged.md").write_text("# Keep this\n")
            git("add", "Home.md", "Unmanaged.md", cwd=seed)
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Init", cwd=seed)
            git("push", "origin", "HEAD", cwd=seed)

            source = root / "authored"
            source.mkdir()
            (source / "doc.md").write_text("# New Doc\n")

            # Dry run reports changes without committing
            dry_run = publisher.publish_wiki("acme/project", source, [("doc.md", "Doc")], str(remote))
            self.assertEqual(dry_run["changes"], ["Doc.md"])

            # Apply writes only Doc.md
            res = publisher.publish_wiki("acme/project", source, [("doc.md", "Doc")], str(remote), apply=True)
            self.assertEqual(res["changes"], ["Doc.md"])

            # Verify unmanaged page still exists in the cloned repo
            check = root / "check"
            git("clone", str(remote), str(check))
            self.assertTrue((check / "Unmanaged.md").exists())
            self.assertTrue((check / "Doc.md").exists())

    def test_projects_v2_static_queries_and_no_dead_queries(self):
        """QUERIES defines static UserProjects/OrganizationProjects and omits dead queries."""
        self.assertIn("UserProjects", projects.QUERIES)
        self.assertIn("OrganizationProjects", projects.QUERIES)
        self.assertIn("owner:user(login:$login)", projects.QUERIES["UserProjects"])
        self.assertIn("owner:organization(login:$login)", projects.QUERIES["OrganizationProjects"])
        for dead in ("OwnerProjects", "IssueBlockedBy", "IssueBlocking", "AddBlockedBy"):
            self.assertNotIn(dead, projects.QUERIES)

    def test_projects_v2_graphql_query_does_not_mutate_variables(self):
        """GitHubGraphQL.query leaves caller variables unmutated for static owner queries."""
        client = projects.GitHubGraphQL("test-token")
        caller_vars = {"login": "octocat"}
        expected_vars = dict(caller_vars)
        fake_response = io.BytesIO(json.dumps({"data": {"owner": {"id": "U1"}}}).encode())
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = lambda s, *args: None
        with mock.patch.object(projects.request, "urlopen", return_value=fake_response) as mocked_open:
            data = client.query("UserProjects", caller_vars)
        self.assertEqual(data, {"owner": {"id": "U1"}})
        self.assertEqual(caller_vars, expected_vars)
        sent = json.loads(mocked_open.call_args[0][0].data.decode())
        self.assertEqual(sent["operationName"], "UserProjects")
        self.assertEqual(sent["variables"], {"login": "octocat"})

    def test_projects_v2_state_selects_explicit_owner_operation(self):
        """_state selects UserProjects or OrganizationProjects without ownerType variable."""
        for owner_type, expected_op in (("user", "UserProjects"), ("organization", "OrganizationProjects")):
            client = FakeGraphQL()
            policy = {
                "version": 1,
                "owner_type": owner_type,
                "owner": "acme",
                "title": "Roadmap",
                "fields": [],
                "items": [],
            }
            projects.reconcile(client, policy, apply=False)
            ops = [op for op, _ in client.calls]
            self.assertIn(expected_op, ops)
            self.assertNotIn("OwnerProjects", ops)
            owner_call_vars = next(v for op, v in client.calls if op == expected_op)
            self.assertEqual(owner_call_vars, {"login": "acme", "after": None})
            self.assertNotIn("ownerType", owner_call_vars)

    def test_projects_v2_update_field_variables_and_option_keys(self):
        """UpdateField sends only {fieldId, options} and options omit id."""
        existing = {
            "options": [
                {"id": "O1", "name": "Todo", "color": "RED", "description": "Old"},
                {"id": "O2", "name": "Keep", "color": "GREEN", "description": "Stay"},
            ]
        }
        desired = [
            {"name": "Todo", "color": "BLUE", "description": "Updated"},
            {"name": "Done", "color": "PURPLE", "description": "New"},
        ]
        merged = projects._desired_option_inputs(desired, existing)
        self.assertEqual([o["name"] for o in merged], ["Todo", "Keep", "Done"])
        for opt in merged:
            self.assertEqual(set(opt.keys()), {"name", "color", "description"})
            self.assertNotIn("id", opt)

        client = FakeGraphQL()
        client.projects.append(
            {
                "id": "P1",
                "title": "Roadmap",
                "url": "https://github.com/users/u/projects/1",
                "items": [],
                "fields": [
                    {
                        "id": "F1",
                        "name": "Status",
                        "dataType": "SINGLE_SELECT",
                        "__typename": "ProjectV2SingleSelectField",
                        "options": existing["options"],
                    }
                ],
            }
        )
        policy = {
            "version": 1,
            "owner_type": "user",
            "owner": "u",
            "title": "Roadmap",
            "fields": [{"name": "Status", "type": "SINGLE_SELECT", "options": desired}],
            "items": [],
        }
        projects.reconcile(client, policy, apply=True)
        update_calls = [v for op, v in client.calls if op == "UpdateField"]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(set(update_calls[0].keys()), {"fieldId", "options"})
        self.assertNotIn("projectId", update_calls[0])
        for opt in update_calls[0]["options"]:
            self.assertEqual(set(opt.keys()), {"name", "color", "description"})
            self.assertNotIn("id", opt)


if __name__ == "__main__":
    unittest.main()
