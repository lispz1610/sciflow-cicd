"""Tests for the shared standard-library GitHub REST client."""

import io
import json
import unittest
from unittest.mock import patch
import urllib.error

from scripts import github_client


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self._raw = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self._raw if size < 0 else self._raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(status, payload, headers=None):
    error = urllib.error.HTTPError(
        "https://api.github.com/test", status, "error", headers or {}, io.BytesIO(json.dumps(payload).encode())
    )
    return error


class SharedClientTests(unittest.TestCase):
    def test_request_sends_github_headers_and_returns_status_body(self):
        client = github_client.GitHubApi("secret-token", user_agent="test-agent")
        with patch("scripts.github_client.urlopen", return_value=FakeResponse({"ok": True})) as urlopen:
            status, body = client.request("GET", "/repos/acme/project")

        self.assertEqual((status, body), (200, {"ok": True}))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/acme/project")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(request.get_header("X-github-api-version"), github_client.API_VERSION)
        self.assertEqual(request.get_header("User-agent"), "test-agent")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], github_client.DEFAULT_TIMEOUT)

    def test_request_with_headers_returns_normalized_headers_and_body(self):
        client = github_client.GitHubApi("token")
        response = FakeResponse({"sha": "abc"}, headers={"X-RateLimit-Remaining": "7"})
        with patch("scripts.github_client.urlopen", return_value=response):
            status, headers, body = client.request_with_headers("GET", "/repos/acme/project")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"sha": "abc"})
        self.assertEqual(headers, {"x-ratelimit-remaining": "7"})

    def test_bounded_response_read_rejects_oversized_payload(self):
        client = github_client.GitHubApi("token", max_response_bytes=16)
        with patch("scripts.github_client.urlopen", return_value=FakeResponse({"data": "x" * 100})):
            with self.assertRaises(github_client.ApiError):
                client.request("GET", "/large")

    def test_http_errors_are_mapped_without_duplicating_error_boilerplate(self):
        client = github_client.GitHubApi("token")
        with patch("scripts.github_client.urlopen", side_effect=http_error(403, {"message": "Forbidden"})):
            status, headers, body = client.request_with_headers("GET", "/repos/acme/project")
        self.assertEqual((status, body), (403, {"message": "Forbidden"}))
        self.assertEqual(headers, {})

        with patch("scripts.github_client.urlopen", side_effect=http_error(403, {"message": "Forbidden"})):
            with self.assertRaisesRegex(github_client.ApiError, "HTTP 403: Forbidden"):
                client.request("GET", "/repos/acme/project")

    def test_get_404_returns_empty_body_for_both_request_flavors(self):
        client = github_client.GitHubApi("token")
        with patch("scripts.github_client.urlopen", side_effect=http_error(404, {"message": "Not Found"})):
            self.assertEqual(client.request("GET", "/missing"), (404, None))
        with patch("scripts.github_client.urlopen", side_effect=http_error(404, {"message": "Not Found"})):
            self.assertEqual(client.request_with_headers("GET", "/missing")[2], None)

    def test_get_pages_collects_every_page(self):
        client = github_client.GitHubApi("token")
        first = [{"number": 1}] * 100
        second = [{"number": 101}]
        with patch.object(client, "request", side_effect=[(200, first), (200, second)]) as request:
            items = github_client.get_pages(client, "/repos/acme/project/issues", key="issues")
        self.assertEqual(len(items), 101)
        self.assertEqual(
            [call.args[1] for call in request.call_args_list],
            ["/repos/acme/project/issues?per_page=100&page=1", "/repos/acme/project/issues?per_page=100&page=2"],
        )


class ConsumerCompatibilityTests(unittest.TestCase):
    def test_configure_github_repo_reexports_shared_client_contract(self):
        from scripts import configure_github_repo

        self.assertIs(configure_github_repo.ApiError, github_client.ApiError)
        self.assertTrue(issubclass(configure_github_repo.GitHubApi, github_client.GitHubApi))
        self.assertIs(configure_github_repo.get_pages, github_client.get_pages)

    def test_polling_client_exposes_request_with_headers(self):
        from scripts import ocr_poller

        client = ocr_poller.PollingGitHubClient("token")
        self.assertTrue(hasattr(client, "request_with_headers"))

    def test_sbom_client_keeps_status_body_semantics(self):
        from scripts import export_release_sbom

        client = export_release_sbom.SbomGitHubApi("token")
        with patch.object(github_client.GitHubApi, "request_with_headers",
                          return_value=(404, {}, {"message": "Not Found"})):
            self.assertEqual(client.request("GET", "/missing"), (404, {"message": "Not Found"}))


class SharedApiTestDoubleContractTests(unittest.TestCase):
    """Test doubles reuse the unified client's transport surface and pagination."""

    def test_shared_doubles_expose_the_unified_transport_contract(self):
        from api_test_support import CleanRepoApi, FakeApi, InventoryApi

        for double_type in (FakeApi, InventoryApi, CleanRepoApi):
            with self.subTest(double=double_type.__name__):
                api = double_type()
                self.assertIsInstance(api, github_client.GitHubApi)
                for method in ("request", "request_with_headers", "paginate"):
                    self.assertTrue(callable(getattr(api, method, None)))
                status, headers, body = api.request_with_headers("GET", "/repos/acme/project")
                self.assertEqual(status, 200)
                self.assertEqual(headers["x-github-api-version"], github_client.API_VERSION)
                self.assertIn("default_branch", body)

    def test_shared_double_paginates_through_its_own_request_route(self):
        from api_test_support import InventoryApi

        class PagedApi(InventoryApi):
            def __init__(self):
                super().__init__()
                self.pages = []

            def request(self, method, path, payload=None):
                if "rulesets?includes_parents=false&per_page=100&page=" in path:
                    self.pages.append(path)
                    if path.endswith("page=1"):
                        return 200, [{"id": index} for index in range(100)]
                    return 200, [{"id": 100}]
                return super().request(method, path, payload)

        api = PagedApi()
        items = github_client.get_pages(api, "/repos/acme/project/rulesets?includes_parents=false")
        self.assertEqual(len(items), 101)
        self.assertEqual(len(api.pages), 2)


if __name__ == "__main__":
    unittest.main()
