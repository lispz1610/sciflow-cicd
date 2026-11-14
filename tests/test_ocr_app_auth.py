"""Tests for limited GitHub App preflight and scoped installation token auth."""

import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.ocr_app_auth import (
    OcrAppAuthError,
    ScopedInstallationToken,
    ScopedInstallationTokenManager,
    format_app_preflight_report,
    generate_app_jwt,
    preflight_ocr_app,
    run_app_preflight,
)


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


class JwtGenerationOpenSslTests(unittest.TestCase):
    def test_generate_app_jwt_signs_valid_rs256_jwt_with_real_openssl_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            priv_key = tmp / "app-private-key.pem"
            pub_key = tmp / "app-public-key.pem"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(priv_key), "2048"],
                check=True,
                capture_output=True,
            )
            os.chmod(priv_key, 0o600)
            subprocess.run(
                ["openssl", "rsa", "-in", str(priv_key), "-pubout", "-out", str(pub_key)],
                check=True,
                capture_output=True,
            )

            fixed_now = 1_760_000_000
            token = generate_app_jwt(
                app_id=987654,
                private_key_path=priv_key,
                now=fixed_now,
                ttl_seconds=540,
            )

            parts = token.split(".")
            self.assertEqual(len(parts), 3, "JWT must have 3 dot-separated segments")
            header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
            payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
            signature = _b64url_decode(parts[2])

            self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
            self.assertEqual(payload["iss"], "987654")
            self.assertEqual(payload["iat"], fixed_now - 60)
            self.assertEqual(payload["exp"], fixed_now + 540)
            self.assertLessEqual(payload["exp"] - payload["iat"], 600)

            signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
            sig_file = tmp / "sig.bin"
            sig_file.write_bytes(signature)
            verify_proc = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", str(pub_key), "-signature", str(sig_file)],
                input=signing_input,
                capture_output=True,
            )
            self.assertEqual(
                verify_proc.returncode,
                0,
                f"OpenSSL signature verification failed: {verify_proc.stderr.decode('utf-8', errors='replace')}",
            )

    def test_generate_app_jwt_rejects_symlinks_and_group_world_readable_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            priv_key = tmp / "key.pem"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(priv_key), "2048"],
                check=True,
                capture_output=True,
            )

            # Group/world-readable permissions must be rejected
            os.chmod(priv_key, 0o644)
            with self.assertRaises(OcrAppAuthError) as ctx:
                generate_app_jwt(app_id=123, private_key_path=priv_key)
            self.assertIn("600", str(ctx.exception))
            self.assertNotIn("PRIVATE KEY", str(ctx.exception))

            # Symlink must be rejected
            os.chmod(priv_key, 0o600)
            symlink_key = tmp / "symlink-key.pem"
            os.symlink(priv_key, symlink_key)
            with self.assertRaises(OcrAppAuthError) as ctx_sym:
                generate_app_jwt(app_id=123, private_key_path=symlink_key)
            self.assertIn("symlink", str(ctx_sym.exception).lower())


class FakeGitHubAppServer:
    """In-memory fake GitHub API responding to App JWT and installation tokens."""

    def __init__(self, app_info, installations, repos_by_installation, token_factory=None):
        self.app_info = app_info
        self.installations = installations
        self.repos_by_installation = repos_by_installation
        self.token_factory = token_factory
        self.calls = []
        self.token_to_installation = {}

    def api_factory(self, bearer_token):
        server = self

        class _Client:
            def request(self, method, path, payload=None):
                server.calls.append((bearer_token, method, path, payload))
                if method == "GET" and path == "/app":
                    return 200, server.app_info
                if method == "GET" and path.startswith("/app/installations"):
                    if "page=1" in path or "?" not in path:
                        return 200, server.installations
                    return 200, []
                if method == "POST" and path.startswith("/app/installations/") and path.endswith("/access_tokens"):
                    inst_id = int(path.split("/")[3])
                    if server.token_factory is not None:
                        return server.token_factory(inst_id, payload)
                    minted = f"ghs_scoped_token_{inst_id}_{len(server.calls)}"
                    server.token_to_installation[minted] = inst_id
                    perms = (payload or {}).get("permissions") or {"metadata": "read"}
                    repos = (payload or {}).get("repositories") or []
                    return 201, {
                        "token": minted,
                        "expires_at": "2026-11-14T01:00:00Z",
                        "permissions": perms,
                        "repositories": [{"name": r} for r in repos],
                    }
                if method == "GET" and path.startswith("/installation/repositories"):
                    inst_id = server.token_to_installation.get(bearer_token)
                    repos = server.repos_by_installation.get(inst_id, [])
                    if "page=1" in path or "?" not in path:
                        return 200, {"total_count": len(repos), "repositories": repos}
                    return 200, {"total_count": len(repos), "repositories": []}
                return 404, None

        return _Client()


class AppPreflightAdminRejectionTests(unittest.TestCase):
    def test_rejects_administrative_client_app_id_before_any_api_calls(self):
        server = FakeGitHubAppServer(
            app_info={"id": 1111, "slug": "gh-setup-admin", "name": "gh-setup", "permissions": {}},
            installations=[],
            repos_by_installation={},
        )
        result = preflight_ocr_app(
            app_id=1111,
            allowlist=["acme/science-repo"],
            admin_app_ids=[1111],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )

        self.assertFalse(result.passed)
        self.assertEqual(server.calls, [], "Rejected admin App ID must not invoke GitHub API")
        self.assertEqual(result.eligible_repositories, [])
        self.assertTrue(any("admin" in err.lower() or "client" in err.lower() for err in result.errors))

    def test_rejects_app_holding_administration_permission_without_minting_tokens(self):
        server = FakeGitHubAppServer(
            app_info={
                "id": 2222,
                "slug": "overprivileged-app",
                "name": "Overprivileged App",
                "permissions": {
                    "administration": "write",
                    "contents": "read",
                    "pull_requests": "write",
                    "metadata": "read",
                },
            },
            installations=[
                {
                    "id": 501,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {
                        "administration": "write",
                        "contents": "read",
                        "pull_requests": "write",
                        "metadata": "read",
                    },
                }
            ],
            repos_by_installation={501: [{"id": 1, "name": "science-repo", "full_name": "acme/science-repo"}]},
        )

        result = preflight_ocr_app(
            app_id=2222,
            allowlist=["acme/science-repo"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.eligible_repositories, [])
        self.assertTrue(any("administration" in err.lower() for err in result.errors))
        post_calls = [c for c in server.calls if c[1] == "POST"]
        self.assertEqual(post_calls, [], "Must never mint installation tokens for an app with administration access")
        serialized = json.dumps(result.to_dict())
        self.assertNotIn("eyJ", serialized, "Result must not expose JWTs")

    def test_rejects_app_and_installation_with_non_ocr_privileged_permissions_unconditionally(self):
        privileged_cases = [
            ("administration", "read"),
            ("secrets", "write"),
            ("secrets", "read"),
            ("environments", "write"),
            ("repository_hooks", "write"),
            ("organization_hooks", "write"),
            ("members", "write"),
            ("organization_administration", "write"),
            ("organization_secrets", "write"),
            ("actions", "write"),
            ("actions", "admin"),
            ("contents", "write"),
            ("workflows", "write"),
        ]
        for perm_name, perm_level in privileged_cases:
            with self.subTest(target="app", perm=perm_name, level=perm_level):
                app_perms = {
                    "contents": "read",
                    "pull_requests": "write",
                    "metadata": "read",
                    perm_name: perm_level,
                }
                server = FakeGitHubAppServer(
                    app_info={
                        "id": 2223,
                        "slug": "privileged-ocr-app",
                        "name": "Privileged App",
                        "permissions": app_perms,
                    },
                    installations=[
                        {
                            "id": 502,
                            "account": {"login": "acme", "type": "Organization"},
                            "repository_selection": "selected",
                            "permissions": {
                                "contents": "read",
                                "pull_requests": "write",
                                "metadata": "read",
                            },
                        }
                    ],
                    repos_by_installation={502: [{"id": 1, "name": "science-repo", "full_name": "acme/science-repo"}]},
                )
                result = preflight_ocr_app(
                    app_id=2223,
                    allowlist=["acme/science-repo"],
                    api_factory=server.api_factory,
                    signer=lambda data: b"fake-sig",
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.eligible_repositories, [])
                self.assertTrue(
                    any(perm_name in err.lower() for err in result.errors),
                    f"Expected {perm_name} in errors, got {result.errors}",
                )
                self.assertEqual([c for c in server.calls if c[1] == "POST"], [])
                self.assertNotIn("eyJ", json.dumps(result.to_dict()))

            with self.subTest(target="installation", perm=perm_name, level=perm_level):
                inst_perms = {
                    "contents": "read",
                    "pull_requests": "write",
                    "metadata": "read",
                    perm_name: perm_level,
                }
                server = FakeGitHubAppServer(
                    app_info={
                        "id": 2224,
                        "slug": "ocr-app-clean-meta",
                        "name": "Clean App Meta",
                        "permissions": {
                            "contents": "read",
                            "pull_requests": "write",
                            "metadata": "read",
                        },
                    },
                    installations=[
                        {
                            "id": 503,
                            "account": {"login": "acme", "type": "Organization"},
                            "repository_selection": "selected",
                            "permissions": inst_perms,
                        }
                    ],
                    repos_by_installation={503: [{"id": 1, "name": "science-repo", "full_name": "acme/science-repo"}]},
                )
                result = preflight_ocr_app(
                    app_id=2224,
                    allowlist=["acme/science-repo"],
                    api_factory=server.api_factory,
                    signer=lambda data: b"fake-sig",
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.eligible_repositories, [])
                self.assertTrue(
                    any(perm_name in err.lower() for err in result.errors),
                    f"Expected {perm_name} in errors, got {result.errors}",
                )
                self.assertEqual([c for c in server.calls if c[1] == "POST"], [])
                self.assertNotIn("eyJ", json.dumps(result.to_dict()))

    def test_allows_actions_read_with_required_ocr_permissions(self):
        perms = {
            "contents": "read",
            "pull_requests": "write",
            "metadata": "read",
            "actions": "read",
        }
        server = FakeGitHubAppServer(
            app_info={"id": 2225, "slug": "ocr-actions-read", "name": "OCR Read", "permissions": perms},
            installations=[
                {
                    "id": 504,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": perms,
                }
            ],
            repos_by_installation={504: [{"id": 1, "name": "science-repo", "full_name": "acme/science-repo"}]},
        )
        result = preflight_ocr_app(
            app_id=2225,
            allowlist=["acme/science-repo"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.eligible_repositories, ["acme/science-repo"])

    def test_rejects_discovery_token_with_forbidden_or_missing_permissions_without_leaking_token(self):
        for bad_perms in (
            {"metadata": "read", "secrets": "write"},
            {},
            None,
        ):
            with self.subTest(bad_perms=bad_perms):
                secret_disc_tok = "ghs_discovery_bad_scope_987"

                def token_factory(inst_id, payload, perms_val=bad_perms):
                    body = {
                        "token": secret_disc_tok,
                        "expires_at": "2026-11-14T01:00:00Z",
                    }
                    if perms_val is not None:
                        body["permissions"] = perms_val
                    return 201, body

                server = FakeGitHubAppServer(
                    app_info={
                        "id": 2226,
                        "slug": "ocr-limited-app",
                        "name": "OCR Limited App",
                        "permissions": {
                            "contents": "read",
                            "pull_requests": "write",
                            "metadata": "read",
                        },
                    },
                    installations=[
                        {
                            "id": 505,
                            "account": {"login": "acme", "type": "Organization"},
                            "repository_selection": "selected",
                            "permissions": {
                                "contents": "read",
                                "pull_requests": "write",
                                "metadata": "read",
                            },
                        }
                    ],
                    repos_by_installation={
                        505: [{"id": 1, "name": "science-repo", "full_name": "acme/science-repo"}]
                    },
                    token_factory=token_factory,
                )
                result = preflight_ocr_app(
                    app_id=2226,
                    allowlist=["acme/science-repo"],
                    api_factory=server.api_factory,
                    signer=lambda data: b"fake-sig",
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.eligible_repositories, [])
                self.assertEqual(
                    [c for c in server.calls if c[2].startswith("/installation/repositories")],
                    [],
                )
                serialized = json.dumps(result.to_dict())
                self.assertNotIn(secret_disc_tok, serialized)
                self.assertNotIn("eyJ", serialized)


class AppPreflightAllowlistAndPermissionsTests(unittest.TestCase):
    def test_evaluates_allowlist_against_installations_and_excludes_inaccessible_or_underprivileged(self):
        server = FakeGitHubAppServer(
            app_info={
                "id": 3333,
                "slug": "ocr-limited-app",
                "name": "OCR Limited App",
                "permissions": {
                    "contents": "read",
                    "pull_requests": "write",
                    "metadata": "read",
                },
            },
            installations=[
                {
                    "id": 701,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {
                        "contents": "read",
                        "pull_requests": "write",
                        "metadata": "read",
                    },
                },
                {
                    "id": 702,
                    "account": {"login": "beta-org", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {
                        "contents": "read",
                        "pull_requests": "read",
                        "metadata": "read",
                    },
                },
            ],
            repos_by_installation={
                701: [
                    {"id": 10, "name": "eligible-repo", "full_name": "acme/eligible-repo"},
                    {"id": 11, "name": "unlisted-repo", "full_name": "acme/unlisted-repo"},
                ],
                702: [
                    {"id": 20, "name": "readonly-pr-repo", "full_name": "beta-org/readonly-pr-repo"},
                ],
            },
        )

        result = preflight_ocr_app(
            app_id=3333,
            allowlist=[
                "acme/eligible-repo",
                "acme/not-installed-repo",
                "beta-org/readonly-pr-repo",
                "gamma-org/no-installation",
            ],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.eligible_repositories, ["acme/eligible-repo"])
        self.assertNotIn("acme/unlisted-repo", result.eligible_repositories)

        excluded_by_repo = {item.repository: item for item in result.excluded_repositories}
        self.assertEqual(
            set(excluded_by_repo.keys()),
            {"acme/not-installed-repo", "beta-org/readonly-pr-repo", "gamma-org/no-installation"},
        )

        self.assertIn("access", excluded_by_repo["acme/not-installed-repo"].reason.lower())
        self.assertIn("acme/not-installed-repo", excluded_by_repo["acme/not-installed-repo"].setup_instruction)

        self.assertIn("pull_requests:write", excluded_by_repo["beta-org/readonly-pr-repo"].reason)
        self.assertIn("pull_requests:write", excluded_by_repo["beta-org/readonly-pr-repo"].setup_instruction)

        self.assertIn("installation", excluded_by_repo["gamma-org/no-installation"].reason.lower())
        self.assertIn("gamma-org/no-installation", excluded_by_repo["gamma-org/no-installation"].setup_instruction)

        # Verify discovery token requests never ask for administration permission
        post_calls = [c for c in server.calls if c[1] == "POST"]
        self.assertTrue(len(post_calls) >= 1)
        for _, _, _, payload in post_calls:
            self.assertNotIn("administration", (payload or {}).get("permissions", {}))

        serialized = json.dumps(result.to_dict())
        self.assertNotIn("ghs_", serialized, "Result must not expose installation tokens")
        self.assertNotIn("eyJ", serialized, "Result must not expose JWTs")

    def test_fails_preflight_when_no_allowlisted_repository_is_eligible(self):
        server = FakeGitHubAppServer(
            app_info={
                "id": 3333,
                "slug": "ocr-limited-app",
                "name": "OCR Limited App",
                "permissions": {
                    "contents": "read",
                    "pull_requests": "write",
                    "metadata": "read",
                },
            },
            installations=[],
            repos_by_installation={},
        )

        result = preflight_ocr_app(
            app_id=3333,
            allowlist=["acme/missing-repo"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.eligible_repositories, [])
        self.assertEqual(len(result.excluded_repositories), 1)
        self.assertTrue(any("no allowlisted" in err.lower() or "eligible" in err.lower() for err in result.errors))


class ScopedInstallationTokenManagerTests(unittest.TestCase):
    def test_mints_caches_and_refreshes_repository_scoped_installation_tokens(self):
        clock = [datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)]
        minted_counter = [0]

        def token_factory(inst_id, payload):
            minted_counter[0] += 1
            expires = clock[0] + timedelta(minutes=10)
            return 201, {
                "token": f"ghs_secret_token_{minted_counter[0]}",
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "permissions": (payload or {}).get("permissions"),
                "repositories": [{"name": r, "full_name": f"acme/{r}"} for r in (payload or {}).get("repositories", [])],
            }

        server = FakeGitHubAppServer(
            app_info={"id": 4444, "slug": "ocr-app", "name": "OCR App", "permissions": {}},
            installations=[],
            repos_by_installation={},
            token_factory=token_factory,
        )

        manager = ScopedInstallationTokenManager(
            app_id=4444,
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            refresh_buffer_seconds=120,
            now_fn=lambda: clock[0],
        )

        # 1. First mint calls POST /app/installations/801/access_tokens with strictly [science-repo]
        tok1 = manager.get_token("acme/science-repo", installation_id=801)
        self.assertIsInstance(tok1, ScopedInstallationToken)
        self.assertEqual(tok1.token, "ghs_secret_token_1")
        self.assertEqual(len(server.calls), 1)
        _, method, path, payload = server.calls[0]
        self.assertEqual((method, path), ("POST", "/app/installations/801/access_tokens"))
        self.assertEqual(payload["repositories"], ["science-repo"])
        self.assertEqual(
            payload["permissions"],
            {"contents": "read", "pull_requests": "write", "metadata": "read"},
        )
        self.assertNotIn("administration", payload["permissions"])

        # Token repr/str must never leak raw secret
        self.assertNotIn("ghs_secret_token_1", repr(tok1))
        self.assertNotIn("ghs_secret_token_1", str(tok1))

        # 2. Still valid (5 min elapsed, 5 min remaining > 120s buffer) -> cache hit
        clock[0] += timedelta(minutes=5)
        tok2 = manager.get_token("acme/science-repo", installation_id=801)
        self.assertEqual(tok2.token, "ghs_secret_token_1")
        self.assertEqual(len(server.calls), 1, "Valid cached token should be reused without a new POST")

        # 3. Near expiration (8m30s elapsed, 90s remaining <= 120s buffer) -> refreshes cleanly
        clock[0] += timedelta(minutes=3, seconds=30)
        tok3 = manager.get_token("acme/science-repo", installation_id=801)
        self.assertEqual(tok3.token, "ghs_secret_token_2")
        self.assertEqual(len(server.calls), 2, "Near-expiry token must trigger refresh POST")

        # 4. Different repository gets its own strictly-scoped token
        tok_other = manager.get_token("acme/other-repo", installation_id=801)
        self.assertEqual(tok_other.token, "ghs_secret_token_3")
        self.assertEqual(server.calls[-1][3]["repositories"], ["other-repo"])

    def test_rejects_minted_token_if_server_returns_administration_or_wrong_scope_without_leaking_token(self):
        def bad_token_factory(inst_id, payload):
            return 201, {
                "token": "ghs_overprivileged_secret_999",
                "expires_at": "2026-11-14T01:00:00Z",
                "permissions": {
                    "administration": "write",
                    "contents": "read",
                    "pull_requests": "write",
                    "metadata": "read",
                },
                "repositories": [{"name": "science-repo", "full_name": "acme/science-repo"}],
            }

        server = FakeGitHubAppServer(
            app_info={},
            installations=[],
            repos_by_installation={},
            token_factory=bad_token_factory,
        )
        manager = ScopedInstallationTokenManager(
            app_id=4444,
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )

        with self.assertRaises(OcrAppAuthError) as ctx:
            manager.get_token("acme/science-repo", installation_id=801)
        self.assertIn("administration", str(ctx.exception).lower())
        self.assertNotIn("ghs_overprivileged_secret_999", str(ctx.exception))

    def test_rejects_minted_token_with_non_ocr_privileged_permissions_without_leaking_token(self):
        privileged_cases = [
            ("administration", "read"),
            ("secrets", "write"),
            ("environments", "write"),
            ("repository_hooks", "write"),
            ("organization_hooks", "write"),
            ("members", "write"),
            ("organization_administration", "write"),
            ("organization_secrets", "write"),
            ("actions", "write"),
            ("actions", "admin"),
            ("contents", "write"),
            ("workflows", "write"),
        ]
        for perm_name, perm_level in privileged_cases:
            with self.subTest(perm=perm_name, level=perm_level):
                secret_tok = f"ghs_priv_tok_{perm_name}_{perm_level}_888"

                def priv_token_factory(inst_id, payload, name=perm_name, level=perm_level, tok=secret_tok):
                    return 201, {
                        "token": tok,
                        "expires_at": "2026-11-14T01:00:00Z",
                        "permissions": {
                            "contents": "read",
                            "pull_requests": "write",
                            "metadata": "read",
                            name: level,
                        },
                        "repositories": [{"name": "science-repo", "full_name": "acme/science-repo"}],
                    }

                server = FakeGitHubAppServer(
                    app_info={},
                    installations=[],
                    repos_by_installation={},
                    token_factory=priv_token_factory,
                )
                manager = ScopedInstallationTokenManager(
                    app_id=4444,
                    api_factory=server.api_factory,
                    signer=lambda data: b"fake-sig",
                )
                with self.assertRaises(OcrAppAuthError) as ctx:
                    manager.get_token("acme/science-repo", installation_id=801)
                self.assertIn(perm_name, str(ctx.exception).lower())
                self.assertNotIn(secret_tok, str(ctx.exception))

    def test_allows_minted_token_with_actions_read_and_required_permissions(self):
        def read_actions_factory(inst_id, payload):
            return 201, {
                "token": "ghs_actions_read_ok_555",
                "expires_at": "2026-11-14T01:00:00Z",
                "permissions": {
                    "contents": "read",
                    "pull_requests": "write",
                    "metadata": "read",
                    "actions": "read",
                },
                "repositories": [{"name": "science-repo", "full_name": "acme/science-repo"}],
            }

        server = FakeGitHubAppServer(
            app_info={},
            installations=[],
            repos_by_installation={},
            token_factory=read_actions_factory,
        )
        manager = ScopedInstallationTokenManager(
            app_id=4444,
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )
        scoped = manager.get_token("acme/science-repo", installation_id=801)
        self.assertEqual(scoped.token, "ghs_actions_read_ok_555")
        self.assertEqual(scoped.permissions["actions"], "read")

    def test_rejects_minted_token_if_repositories_scope_mismatches_or_insecure_api_url(self):
        def wrong_repo_factory(inst_id, payload):
            return 201, {
                "token": "ghs_wrong_scope_secret_777",
                "expires_at": "2026-11-14T01:00:00Z",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                "repositories": [{"name": "different-repo", "full_name": "acme/different-repo"}],
            }

        server = FakeGitHubAppServer(
            app_info={},
            installations=[],
            repos_by_installation={},
            token_factory=wrong_repo_factory,
        )
        manager = ScopedInstallationTokenManager(
            app_id=4444,
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )
        with self.assertRaises(OcrAppAuthError) as ctx:
            manager.get_token("acme/science-repo", installation_id=801)
        self.assertIn("selected repository", str(ctx.exception).lower())
        self.assertNotIn("ghs_wrong_scope_secret_777", str(ctx.exception))

        with self.assertRaises(OcrAppAuthError) as ctx_url:
            preflight_ocr_app(
                app_id=4444,
                allowlist=["acme/science-repo"],
                api_base_url="http://api.github.com",
                signer=lambda data: b"fake-sig",
            )
        self.assertIn("https", str(ctx_url.exception).lower())

    def test_rejects_minted_token_when_permissions_omitted_without_leaking_token(self):
        def missing_perms_factory(inst_id, payload):
            return 201, {
                "token": "ghs_missing_perms_secret_111",
                "expires_at": "2026-11-14T01:00:00Z",
                "repositories": [{"name": "science-repo", "full_name": "acme/science-repo"}],
            }

        server = FakeGitHubAppServer(
            app_info={},
            installations=[],
            repos_by_installation={},
            token_factory=missing_perms_factory,
        )
        manager = ScopedInstallationTokenManager(
            app_id=4444,
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )
        with self.assertRaises(OcrAppAuthError) as ctx:
            manager.get_token("acme/science-repo", installation_id=801)
        self.assertIn("permissions", str(ctx.exception).lower())
        self.assertNotIn("ghs_missing_perms_secret_111", str(ctx.exception))

    def test_rejects_minted_token_when_permissions_empty_without_leaking_token(self):
        for empty_perms in ({}, None, []):
            with self.subTest(empty_perms=empty_perms):
                def empty_perms_factory(inst_id, payload, perms_value=empty_perms):
                    return 201, {
                        "token": "ghs_empty_perms_secret_222",
                        "expires_at": "2026-11-14T01:00:00Z",
                        "permissions": perms_value,
                        "repositories": [{"name": "science-repo", "full_name": "acme/science-repo"}],
                    }

                server = FakeGitHubAppServer(
                    app_info={},
                    installations=[],
                    repos_by_installation={},
                    token_factory=empty_perms_factory,
                )
                manager = ScopedInstallationTokenManager(
                    app_id=4444,
                    api_factory=server.api_factory,
                    signer=lambda data: b"fake-sig",
                )
                with self.assertRaises(OcrAppAuthError) as ctx:
                    manager.get_token("acme/science-repo", installation_id=801)
                self.assertIn("permissions", str(ctx.exception).lower())
                self.assertNotIn("ghs_empty_perms_secret_222", str(ctx.exception))


class CliAndReportPreflightTests(unittest.TestCase):
    def test_gh_setup_ocr_preflight_app_end_to_end_with_openssl_key_and_loopback_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            priv_key = tmp / "ocr-app.pem"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(priv_key), "2048"],
                check=True,
                capture_output=True,
            )
            os.chmod(priv_key, 0o600)

            observed_auth_headers = []

            class _Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    return

                def do_GET(self):
                    auth = self.headers.get("Authorization", "")
                    observed_auth_headers.append(auth)
                    if self.path == "/app":
                        body = {
                            "id": 7777,
                            "slug": "ocr-limited-app",
                            "name": "OCR Limited App",
                            "permissions": {
                                "contents": "read",
                                "pull_requests": "write",
                                "metadata": "read",
                            },
                        }
                        status = 200
                    elif self.path.startswith("/app/installations"):
                        body = [
                            {
                                "id": 901,
                                "account": {"login": "acme", "type": "Organization"},
                                "repository_selection": "selected",
                                "permissions": {
                                    "contents": "read",
                                    "pull_requests": "write",
                                    "metadata": "read",
                                },
                            }
                        ]
                        status = 200
                    elif self.path.startswith("/installation/repositories"):
                        body = {
                            "total_count": 1,
                            "repositories": [
                                {"id": 1, "name": "eligible-repo", "full_name": "acme/eligible-repo"}
                            ],
                        }
                        status = 200
                    else:
                        body = {"message": "Not Found"}
                        status = 404
                    raw = json.dumps(body).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def do_POST(self):
                    auth = self.headers.get("Authorization", "")
                    observed_auth_headers.append(auth)
                    body = {
                        "token": "ghs_ephemeral_discovery_secret_123",
                        "expires_at": "2026-11-14T01:00:00Z",
                        "permissions": {"metadata": "read"},
                    }
                    raw = json.dumps(body).encode("utf-8")
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            server = HTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                proc = subprocess.run(
                    [
                        str(ROOT_DIR / "gh-setup"),
                        "ocr",
                        "preflight-app",
                        "--app-id",
                        "7777",
                        "--private-key-path",
                        str(priv_key),
                        "--allowlist",
                        "acme/eligible-repo,acme/missing-repo",
                        "--api-base-url",
                        base_url,
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                )
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}")
            data = json.loads(proc.stdout)
            self.assertTrue(data["passed"])
            self.assertEqual(data["eligible_repositories"], ["acme/eligible-repo"])
            self.assertEqual(len(data["excluded_repositories"]), 1)
            self.assertEqual(data["excluded_repositories"][0]["repository"], "acme/missing-repo")
            self.assertNotIn("ghs_ephemeral_discovery_secret_123", proc.stdout)
            self.assertNotIn("eyJ", proc.stdout)
            self.assertNotIn("PRIVATE KEY", proc.stdout + proc.stderr)
            self.assertTrue(any(h.startswith("Bearer eyJ") for h in observed_auth_headers))

    def test_run_app_preflight_text_report_and_admin_env_rejection(self):
        out = io.StringIO()
        err = io.StringIO()
        with patch_env({"GH_SETUP_ADMIN_APP_ID": "9999"}):
            rc = run_app_preflight(
                app_id="9999",
                private_key_path="/nonexistent/key.pem",
                allowlist=["acme/repo-a"],
                out_stream=out,
                err_stream=err,
            )
        self.assertEqual(rc, 1)
        combined = out.getvalue() + err.getvalue()
        self.assertIn("FAIL", combined)
        self.assertIn("administrative", combined.lower())

        # Also check format_app_preflight_report directly
        result = preflight_ocr_app(
            app_id=9999,
            allowlist=["acme/repo-a"],
            admin_app_ids=[9999],
        )
        report = format_app_preflight_report(result)
        self.assertIn("FAIL", report)
        self.assertNotIn("eyJ", report)

        # Also verify GH_SETUP_CLIENT_APP_ID env var rejection remains intact
        out_client = io.StringIO()
        err_client = io.StringIO()
        with patch_env({"GH_SETUP_CLIENT_APP_ID": "8888"}):
            rc_client = run_app_preflight(
                app_id="8888",
                private_key_path="/nonexistent/key.pem",
                allowlist=["acme/repo-a"],
                out_stream=out_client,
                err_stream=err_client,
            )
        self.assertEqual(rc_client, 1)
        self.assertIn("administrative", (out_client.getvalue() + err_client.getvalue()).lower())

    def test_run_app_preflight_redacts_env_secrets_in_json_and_text_errors(self):
        secret = "custom-env-llm-secret-12345"
        with patch_env({"OCR_LLM_AUTH_TOKEN": secret}):
            out_text = io.StringIO()
            err_text = io.StringIO()
            rc_text = run_app_preflight(
                app_id="4444",
                private_key_path=f"/nonexistent/{secret}/key.pem",
                allowlist=["acme/repo-a"],
                out_stream=out_text,
                err_stream=err_text,
            )
            self.assertEqual(rc_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertNotIn(secret, combined_text)
            self.assertIn("[REDACTED]", combined_text)

            out_json = io.StringIO()
            rc_json = run_app_preflight(
                app_id="4444",
                private_key_path=f"/nonexistent/{secret}/key.pem",
                allowlist=["acme/repo-a"],
                json_output=True,
                out_stream=out_json,
            )
            self.assertEqual(rc_json, 1)
            self.assertNotIn(secret, out_json.getvalue())
            self.assertIn("[REDACTED]", out_json.getvalue())


class OcrAppConfigAndSharedHelpersTests(unittest.TestCase):
    def test_ocr_app_config_is_immutable_and_supports_preflight_and_token_manager(self):
        from dataclasses import FrozenInstanceError
        from scripts.ocr_app_auth import OcrAppConfig

        cfg = OcrAppConfig(
            app_id=4444,
            allowlist=("acme/repo-a",),
            admin_app_ids=("9999",),
            signer=lambda data: b"sig",
        )
        with self.assertRaises(FrozenInstanceError):
            cfg.app_id = 5555  # type: ignore[misc]

        class _FakeClient:
            def __init__(self, token):
                self.token = token

            def request(self, method, path, payload=None):
                if path == "/app":
                    return 200, {
                        "id": 4444,
                        "slug": "ocr-bot",
                        "name": "OCR Bot",
                        "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                    }
                if path.startswith("/app/installations?"):
                    return 200, [
                        {
                            "id": 77,
                            "account": {"login": "acme"},
                            "repository_selection": "selected",
                            "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                        }
                    ]
                if path.startswith("/installation/repositories?"):
                    return 200, {"repositories": [{"full_name": "acme/repo-a"}]}
                if path == "/app/installations/77/access_tokens":
                    return 201, {
                        "token": "ghs_cfg_token_123",
                        "expires_at": "2026-11-14T05:00:00Z",
                        "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                        "repositories": [{"full_name": "acme/repo-a"}],
                    }
                return 404, {"message": "Not Found"}

        cfg_with_factory = OcrAppConfig(
            app_id=4444,
            allowlist=("acme/repo-a",),
            admin_app_ids=("9999",),
            api_factory=lambda token: _FakeClient(token),
            signer=lambda data: b"sig",
        )
        res = preflight_ocr_app(app_config=cfg_with_factory)
        self.assertTrue(res.passed)
        self.assertEqual(res.eligible_repositories, ["acme/repo-a"])

        mgr = ScopedInstallationTokenManager(app_config=cfg_with_factory)
        tok = mgr.get_token("acme/repo-a", installation_id=77)
        self.assertEqual(tok.token, "ghs_cfg_token_123")

    def test_collect_env_secrets_and_client_request_and_no_legacy_private_aliases(self):
        import scripts.ocr_app_auth as ocr_app_auth
        from scripts.ocr_app_auth import client_request, collect_env_secrets

        secrets = collect_env_secrets(
            {
                "OCR_LLM_AUTH_TOKEN": "llm-secret",
                "RUNNER_REGISTRATION_TOKEN": "",
                "ACTIONS_RUNNER_INPUT_TOKEN": "runner-input-secret",
                "UNRELATED": "ignored",
            }
        )
        self.assertEqual(secrets, ["llm-secret", "runner-input-secret"])

        class _ClientWithHeaders:
            def request_with_headers(self, method, path, payload=None):
                return 200, {"X-Test": "1"}, {"method": method, "path": path, "payload": payload}

        status, body = client_request(
            _ClientWithHeaders(),
            "POST",
            "/repos/acme/repo-a/issues/1/comments",
            payload={"body": "hi"},
            path_validator=lambda p: p,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["payload"], {"body": "hi"})

        for dead_alias in (
            "_REPO_PATTERN",
            "_normalize_app_id",
            "_validate_api_base_url",
            "_default_api_factory",
            "_split_csv_values",
        ):
            self.assertFalse(hasattr(ocr_app_auth, dead_alias), f"Dead alias still present: {dead_alias}")


class patch_env:
    def __init__(self, updates):
        self.updates = updates
        self.saved = {}

    def __enter__(self):
        for k, v in self.updates.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, exc_type, exc, tb):
        for k, prev in self.saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


if __name__ == "__main__":
    unittest.main()
