"""Tests for shared YAML subset parsing and credential redaction."""

import json
import unittest

from scripts import doi_guidance, ocr_app_auth, secret_redaction, verify_release, yaml_subset
from scripts.verify_agent_skill_discovery import SkillDiscoveryError, parse_frontmatter


class YamlSubsetTests(unittest.TestCase):
    def test_doubled_single_quotes_are_scanned_as_an_escape(self):
        parsed = yaml_subset.parse_yaml_subset("message: 'it''s # fine' # trailing note")
        self.assertEqual(parsed["message"], "it's # fine")

    def test_folded_scalars_preserve_paragraph_breaks(self):
        text = "summary: >\n  first line\n  second line\n\n  second paragraph\n"
        self.assertEqual(yaml_subset.parse_yaml_subset(text)["summary"],
                         "first line second line\nsecond paragraph\n")

        stripped = "summary: >-\n  first line\n\n  second paragraph\n"
        self.assertEqual(yaml_subset.parse_yaml_subset(stripped)["summary"],
                         "first line\nsecond paragraph")

    def test_bom_prefixed_frontmatter_is_accepted(self):
        content = "\ufeff---\nname: shared-fixture\n---\nBody text\n"
        frontmatter, body = parse_frontmatter(content)
        self.assertEqual(frontmatter["name"], "shared-fixture")
        self.assertEqual(body, "Body text\n")

    def test_malformed_frontmatter_line_is_rejected(self):
        content = "---\nname: shared-fixture\nthis line has no colon\n---\nBody\n"
        with self.assertRaises(SkillDiscoveryError):
            parse_frontmatter(content)

    def test_cff_inline_comments_and_flow_sequences_use_shared_parser(self):
        text = (
            'cff-version: 1.2.0 # schema version\n'
            'message: "Please cite this software" # guidance\n'
            'authors: [{family-names: Doe, given-names: Jane}, {family-names: Roe}] # people\n'
        )
        metadata = doi_guidance.parse_cff_document(text)
        self.assertEqual(metadata["cff-version"], "1.2.0")
        self.assertEqual(metadata["message"], "Please cite this software")
        self.assertEqual(len(metadata["authors"]), 2)
        self.assertEqual(metadata["authors"][0]["family-names"], "Doe")
        keys = doi_guidance.parse_cff_scalars(text)
        self.assertEqual(keys["message"], "Please cite this software")
        self.assertNotIn("#", keys["message"])
        self.assertEqual(keys["authors"], "")
        self.assertEqual(len(doi_guidance.parse_cff_authors(text)), 2)

    def test_cff_block_authors_are_read_through_shared_parser(self):
        text = (
            "authors:\n"
            "  - family-names: Doe\n"
            "    given-names: Jane\n"
        )
        authors = doi_guidance.parse_cff_authors(text)
        self.assertEqual(authors[0]["given-names"], "Jane")


class SecretRedactionTests(unittest.TestCase):
    def test_all_credential_formats_are_redacted_through_one_helper(self):
        text = "\n".join([
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIEowIBAAKCAQEA",
            "-----END RSA PRIVATE KEY-----",
            "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123def456",
            "github=ghp_abcdefghijklmnop",
            "fine=github_pat_abcdefghijklmnop",
            "openai=sk-abcdefghijklmnop",
            "cloud=nvapi-abcdefghijklmnop",
            "aws=AKIAIOSFODNN7EXAMPLE",
            "Authorization: Bearer secret-token-value",
            "bearer another-secret-token",
            "url=https://user:password@example.com/path",
            "api_key: abcd1234efgh5678",
        ])
        redacted = secret_redaction.redact(text, extra_secrets=["exact-secret-value"])
        self.assertNotIn("MIIEowIBAAKCAQEA", redacted)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", redacted)
        for token in ("ghp_abcdefghijklmnop", "github_pat_abcdefghijklmnop", "sk-abcdefghijklmnop",
                      "nvapi-abcdefghijklmnop", "AKIAIOSFODNN7EXAMPLE", "secret-token-value",
                      "another-secret-token", "user:password", "abcd1234efgh5678", "exact-secret-value"):
            self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_consumers_delegate_to_the_shared_pattern_set(self):
        sample = "token ghp_abcdefghijklmnop"
        self.assertEqual(ocr_app_auth.redact_secrets(sample), secret_redaction.redact(sample))
        self.assertEqual(verify_release.sanitize_credentials(sample), secret_redaction.redact(sample))
        self.assertIs(verify_release.SECRET_PATTERNS, secret_redaction.SECRET_PATTERNS)
        self.assertIs(verify_release.URL_CREDENTIAL_PATTERN, secret_redaction.URL_CREDENTIAL_PATTERN)

    def test_release_record_scan_uses_the_shared_secret_threshold(self):
        record = json.dumps({"version": 1, "status": "token ghp_123456"})
        with self.assertRaises(verify_release.RecordValidationError):
            verify_release.parse_release_record(record)


if __name__ == "__main__":
    unittest.main()
