"""Tests for scripts/verify_issue_delivery.py."""

import unittest

from scripts.verify_issue_delivery import _section, verify_issue_delivery


class FakeDeliveryApi:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def request(self, method, path):
        self.paths.append(path)
        return self.responses[path]


class VerifyIssueDeliveryTests(unittest.TestCase):
    def test_section_supports_h1_to_h6_headings_and_h3_boundaries(self):
        body = (
            "### Delivery\n"
            "- PR: https://github.com/owner/repo/pull/12\n"
            "- SHA: " + ("a" * 40) + "\n"
            "### Notes\n"
            "- https://github.com/owner/repo/pull/99\n"
        )
        found, content = _section(body, "Delivery")
        self.assertTrue(found)
        self.assertIn("pull/12", content)
        self.assertNotIn("pull/99", content)

    def test_verify_issue_delivery_accepts_h3_delivery_and_h3_implementation_issues(self):
        sha = "b" * 40
        api = FakeDeliveryApi({
            "/repos/owner/repo/issues/42": (
                200,
                {
                    "number": 42,
                    "state": "open",
                    "body": (
                        "### Delivery\n"
                        "- https://github.com/owner/repo/pull/7\n"
                        f"- `{sha}`\n"
                        "### Other\n"
                        "ignored\n"
                    ),
                },
            ),
            "/repos/owner/repo/pulls/7": (
                200,
                {
                    "state": "open",
                    "merged_at": None,
                    "draft": False,
                    "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
                    "head": {"ref": "feat/42", "sha": sha, "repo": {"full_name": "owner/repo"}},
                    "body": "### Implementation issues\n- #42\n### Testing\n- #99\n",
                },
            ),
            "/repos/owner/repo/branches/feat%2F42": (
                200,
                {"commit": {"sha": sha}},
            ),
        })
        result = verify_issue_delivery(api, "owner/repo", 42)
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["pull_request"], 7)

    def test_verify_issue_delivery_rejects_dotdot_repository(self):
        api = FakeDeliveryApi({})
        result = verify_issue_delivery(api, "../repo", 1)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["code"], "api_failure")
        self.assertEqual(api.paths, [])


if __name__ == "__main__":
    unittest.main()
