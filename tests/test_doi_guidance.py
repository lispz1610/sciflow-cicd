"""Unit tests for DOI guidance state machine, validation, and metadata integration."""

import unittest
from pathlib import Path

from scripts.doi_guidance import (
    DOI_PATTERN,
    DoiError,
    DoiState,
    DoiStateMachine,
    VALID_TRANSITIONS,
    format_doi_release_notes,
    parse_cff_metadata,
    parse_cff_scalars,
    update_citation_cff_doi,
    validate_citation_cff_doi,
    validate_doi,
    validate_release_doi_metadata,
)

ROOT = Path(__file__).resolve().parents[1]


class DoiStateEnumTests(unittest.TestCase):
    def test_enum_has_all_five_canonical_states(self):
        expected_states = {
            "UNSELECTED": "unselected",
            "REQUESTED": "requested",
            "PENDING": "pending",
            "ISSUED": "issued",
            "UNAVAILABLE": "unavailable",
        }
        for name, value in expected_states.items():
            state = getattr(DoiState, name, None)
            self.assertIsNotNone(state, f"DoiState missing {name}")
            self.assertEqual(state.value, value)


class DoiPatternValidationTests(unittest.TestCase):
    def test_valid_zenodo_and_datacite_dois(self):
        valid_dois = [
            "10.5281/zenodo.1234567",
            "10.5281/zenodo.10000000",
            "10.5061/dryad.m379v",
            "10.1000/182",
            "10.12345/my-dataset-v1.0",
        ]
        for doi in valid_dois:
            with self.subTest(doi=doi):
                self.assertEqual(validate_doi(doi), doi)
                self.assertTrue(bool(DOI_PATTERN.fullmatch(doi)))

    def test_invalid_dois_raise_error(self):
        invalid_dois = [
            "",
            "   ",
            "not-a-doi",
            "https://doi.org/10.5281/zenodo.1234567",  # Full URL, not DOI string
            "doi:10.5281/zenodo.1234567",  # Prefix scheme not allowed in bare DOI
            "11.5281/zenodo.1234567",  # Invalid prefix
            "10.123/zenodo.1234567",  # Prefix registrant must be 4-9 digits
            "10.5281",  # Missing suffix
            "10.5281/",  # Empty suffix
            "10.5281/zenodo pending",  # Space inside DOI
        ]
        for doi in invalid_dois:
            with self.subTest(doi=doi):
                with self.assertRaises(DoiError):
                    validate_doi(doi)


class DoiStateMachineTransitionTests(unittest.TestCase):
    def test_initial_state_defaults_to_unselected(self):
        sm = DoiStateMachine()
        self.assertEqual(sm.state, DoiState.UNSELECTED)
        self.assertIsNone(sm.doi)

    def test_valid_lifecycle_transitions(self):
        # UNSELECTED -> REQUESTED -> PENDING -> ISSUED
        sm = DoiStateMachine()
        sm = sm.transition(DoiState.REQUESTED)
        self.assertEqual(sm.state, DoiState.REQUESTED)
        self.assertIsNone(sm.doi)

        sm = sm.transition(DoiState.PENDING)
        self.assertEqual(sm.state, DoiState.PENDING)
        self.assertIsNone(sm.doi)

        sm = sm.transition(DoiState.ISSUED, doi="10.5281/zenodo.1234567")
        self.assertEqual(sm.state, DoiState.ISSUED)
        self.assertEqual(sm.doi, "10.5281/zenodo.1234567")

    def test_unselected_cannot_jump_directly_to_issued(self):
        sm = DoiStateMachine(DoiState.UNSELECTED)
        with self.assertRaisesRegex(DoiError, "Invalid transition"):
            sm.transition(DoiState.ISSUED, doi="10.5281/zenodo.1234567")

    def test_cannot_record_doi_on_non_issued_states(self):
        for state in (DoiState.UNSELECTED, DoiState.REQUESTED, DoiState.PENDING, DoiState.UNAVAILABLE):
            with self.subTest(state=state):
                # Cannot initialize with DOI
                with self.assertRaisesRegex(DoiError, "cannot be recorded"):
                    DoiStateMachine(state, doi="10.5281/zenodo.1234567")

        # Cannot transition to non-issued state with DOI
        sm = DoiStateMachine(DoiState.UNSELECTED)
        with self.assertRaisesRegex(DoiError, "cannot be recorded"):
            sm.transition(DoiState.REQUESTED, doi="10.5281/zenodo.1234567")

    def test_transition_to_issued_requires_valid_doi(self):
        sm = DoiStateMachine(DoiState.PENDING)
        with self.assertRaisesRegex(DoiError, "requires a verified DOI"):
            sm.transition(DoiState.ISSUED)

        with self.assertRaisesRegex(DoiError, "Invalid DOI format"):
            sm.transition(DoiState.ISSUED, doi="invalid-doi")

    def test_transitions_to_unavailable(self):
        # From REQUESTED to UNAVAILABLE
        sm = DoiStateMachine(DoiState.REQUESTED)
        sm = sm.transition(DoiState.UNAVAILABLE)
        self.assertEqual(sm.state, DoiState.UNAVAILABLE)

        # From PENDING to UNAVAILABLE
        sm = DoiStateMachine(DoiState.PENDING)
        sm = sm.transition(DoiState.UNAVAILABLE)
        self.assertEqual(sm.state, DoiState.UNAVAILABLE)

        # From UNAVAILABLE back to REQUESTED (retry)
        sm = sm.transition(DoiState.REQUESTED)
        self.assertEqual(sm.state, DoiState.REQUESTED)

        # From UNAVAILABLE back to UNSELECTED
        sm = DoiStateMachine(DoiState.UNAVAILABLE)
        sm = sm.transition(DoiState.UNSELECTED)
        self.assertEqual(sm.state, DoiState.UNSELECTED)

    def test_complete_transition_matrix(self):
        valid_pairs = set()
        for src, targets in VALID_TRANSITIONS.items():
            for tgt in targets:
                valid_pairs.add((src, tgt))

        sample_doi = "10.5281/zenodo.1234567"
        for src in DoiState:
            for tgt in DoiState:
                with self.subTest(source=src.value, target=tgt.value):
                    src_doi = sample_doi if src == DoiState.ISSUED else None
                    tgt_doi = sample_doi if tgt == DoiState.ISSUED else None
                    sm = DoiStateMachine(src, doi=src_doi)

                    if (src, tgt) in valid_pairs:
                        self.assertTrue(sm.can_transition_to(tgt))
                        next_sm = sm.transition(tgt, doi=tgt_doi)
                        self.assertEqual(next_sm.state, tgt)
                        if tgt == DoiState.ISSUED:
                            self.assertEqual(next_sm.doi, sample_doi)
                        else:
                            self.assertIsNone(next_sm.doi)
                    else:
                        self.assertFalse(sm.can_transition_to(tgt))
                        with self.assertRaisesRegex(DoiError, "Invalid transition"):
                            sm.transition(tgt, doi=tgt_doi)


class CitationMetadataIntegrationTests(unittest.TestCase):
    SAMPLE_CFF_WITHOUT_DOI = (
        "cff-version: 1.2.0\n"
        "message: \"If you use this software, please cite it as below.\"\n"
        "title: \"Research Tool\"\n"
        "version: 1.0.0\n"
        "date-released: 2026-11-09\n"
        "authors:\n"
        "  - family-names: Turing\n"
        "    given-names: Alan\n"
    )

    SAMPLE_CFF_WITH_DOI = (
        "cff-version: 1.2.0\n"
        "message: \"If you use this software, please cite it as below.\"\n"
        "title: \"Research Tool\"\n"
        "version: 1.0.0\n"
        "date-released: 2026-11-09\n"
        "doi: 10.5281/zenodo.1234567\n"
        "authors:\n"
        "  - family-names: Turing\n"
        "    given-names: Alan\n"
    )

    def test_refuses_unverified_doi_in_cff_when_not_issued(self):
        for state in (DoiState.UNSELECTED, DoiState.REQUESTED, DoiState.PENDING, DoiState.UNAVAILABLE):
            with self.subTest(state=state):
                with self.assertRaisesRegex(DoiError, "unverified DOI"):
                    validate_citation_cff_doi(self.SAMPLE_CFF_WITH_DOI, doi_state=state)

    def test_allows_cff_without_doi_when_not_issued(self):
        for state in (DoiState.UNSELECTED, DoiState.REQUESTED, DoiState.PENDING, DoiState.UNAVAILABLE):
            with self.subTest(state=state):
                doi = validate_citation_cff_doi(self.SAMPLE_CFF_WITHOUT_DOI, doi_state=state)
                self.assertIsNone(doi)

    def test_requires_matching_verified_doi_when_issued(self):
        # Missing DOI when ISSUED raises error
        with self.assertRaisesRegex(DoiError, "missing required 'doi'"):
            validate_citation_cff_doi(self.SAMPLE_CFF_WITHOUT_DOI, doi_state=DoiState.ISSUED, expected_doi="10.5281/zenodo.1234567")

        # Mismatched DOI raises error
        with self.assertRaisesRegex(DoiError, "does not match"):
            validate_citation_cff_doi(self.SAMPLE_CFF_WITH_DOI, doi_state=DoiState.ISSUED, expected_doi="10.5281/zenodo.9999999")

        # Matching verified DOI succeeds
        verified = validate_citation_cff_doi(self.SAMPLE_CFF_WITH_DOI, doi_state=DoiState.ISSUED, expected_doi="10.5281/zenodo.1234567")
        self.assertEqual(verified, "10.5281/zenodo.1234567")

    def test_update_citation_cff_doi_injects_or_strips(self):
        # Strips DOI for non-ISSUED states
        cleaned = update_citation_cff_doi(self.SAMPLE_CFF_WITH_DOI, doi_state=DoiState.REQUESTED)
        self.assertNotIn("doi:", cleaned)
        self.assertIn("title: \"Research Tool\"", cleaned)

        # Injects DOI for ISSUED state
        injected = update_citation_cff_doi(
            self.SAMPLE_CFF_WITHOUT_DOI,
            doi_state=DoiState.ISSUED,
            doi="10.5281/zenodo.1234567",
        )
        self.assertIn("doi: 10.5281/zenodo.1234567", injected)


class ReleaseMetadataIntegrationTests(unittest.TestCase):
    def test_validates_release_doi_metadata_contract(self):
        # Valid non-issued payload (no doi field or doi is None)
        valid_unselected = {"doi_state": "unselected"}
        self.assertEqual(validate_release_doi_metadata(valid_unselected), ("unselected", None))

        valid_pending = {"doi_state": "pending", "doi": None}
        self.assertEqual(validate_release_doi_metadata(valid_pending), ("pending", None))

        # Invalid non-issued payload containing a DOI string
        invalid_pending = {"doi_state": "pending", "doi": "10.5281/zenodo.1234567"}
        with self.assertRaisesRegex(DoiError, "cannot be recorded"):
            validate_release_doi_metadata(invalid_pending)

        # Valid issued payload
        valid_issued = {"doi_state": "issued", "doi": "10.5281/zenodo.1234567"}
        self.assertEqual(validate_release_doi_metadata(valid_issued), ("issued", "10.5281/zenodo.1234567"))

        # Invalid issued payload without doi
        invalid_issued = {"doi_state": "issued"}
        with self.assertRaisesRegex(DoiError, "requires a verified DOI"):
            validate_release_doi_metadata(invalid_issued)

    def test_format_doi_release_notes_notes_distinction(self):
        # UNSELECTED notes
        notes_unselected = format_doi_release_notes(DoiState.UNSELECTED)
        self.assertIn("DOI registration: unselected", notes_unselected)
        self.assertNotIn("10.", notes_unselected)

        # PENDING notes
        notes_pending = format_doi_release_notes(DoiState.PENDING)
        self.assertIn("pending verification", notes_pending)
        self.assertNotIn("10.", notes_pending)

        # ISSUED notes
        notes_issued = format_doi_release_notes(DoiState.ISSUED, doi="10.5281/zenodo.1234567")
        self.assertIn("DOI: `10.5281/zenodo.1234567`", notes_issued)
        self.assertIn("https://doi.org/10.5281/zenodo.1234567", notes_issued)


class CffParsingTests(unittest.TestCase):
    SAMPLE_CFF = """cff-version: 1.2.0
message: 'If you use this software, please cite it as below.'
title: "My Scientific Module"
version: 1.0.0
date-released: 2026-11-11
url: https://github.com/example/module
authors:
  - family-names: Doe
    given-names: Jane
"""

    def test_parse_cff_scalars_parses_unquoted_and_quoted_values(self):
        keys = parse_cff_scalars(self.SAMPLE_CFF)
        self.assertEqual(keys["cff-version"], "1.2.0")
        self.assertEqual(keys["message"], "If you use this software, please cite it as below.")
        self.assertEqual(keys["title"], "My Scientific Module")
        self.assertEqual(keys["version"], "1.0.0")
        self.assertEqual(keys["date-released"], "2026-11-11")
        self.assertEqual(keys["url"], "https://github.com/example/module")
        self.assertEqual(keys["authors"], "")
        self.assertNotIn("family-names", keys)
        self.assertNotIn("given-names", keys)

    def test_parse_cff_metadata_with_string_and_path(self, tmp_path=None):
        import tempfile
        # Test with string
        text, keys = parse_cff_metadata(self.SAMPLE_CFF)
        self.assertEqual(text, self.SAMPLE_CFF)
        self.assertEqual(keys["title"], "My Scientific Module")

        # Test with Path
        with tempfile.TemporaryDirectory() as tmp:
            cff_file = Path(tmp) / "CITATION.cff"
            cff_file.write_text(self.SAMPLE_CFF, encoding="utf-8")
            text_from_file, keys_from_file = parse_cff_metadata(cff_file)
            self.assertEqual(text_from_file, self.SAMPLE_CFF)
            self.assertEqual(keys_from_file["version"], "1.0.0")


class DoiDocumentationFreshnessTests(unittest.TestCase):
    def test_guide_documentation_exists_and_covers_requirements(self):
        guide_path = ROOT / "docs" / "doi-guidance.md"
        self.assertTrue(guide_path.is_file(), f"Expected guide at {guide_path}")
        content = guide_path.read_text(encoding="utf-8")

        # Must distinguish all 5 paths
        for path_name in ("Requested", "Pending", "Issued", "Unavailable", "Unselected"):
            self.assertIn(path_name, content)

        # Must cover public Zenodo integration
        self.assertIn("Zenodo", content)
        self.assertIn("webhook", content.lower())
        self.assertIn("concept doi", content.lower())
        self.assertIn("version doi", content.lower())

        # Must cover private-source manual deposit
        self.assertIn("manual deposit", content.lower())
        self.assertIn("tarball", content.lower())
        self.assertIn("embargo", content.lower())

        # Must state clear rule that DOI is never recorded before issuance is verified
        self.assertIn("never recorded", content.lower())
        self.assertIn("issuance is verified", content.lower())

    def test_release_evidence_links_doi_guidance(self):
        evidence_doc = ROOT / "docs" / "release-evidence.md"
        self.assertTrue(evidence_doc.is_file())
        text = evidence_doc.read_text(encoding="utf-8")
        self.assertIn("doi-guidance.md", text)

    def test_guide_does_not_use_prohibited_release_record_phrase(self):
        guide_path = ROOT / "docs" / "doi-guidance.md"
        content = guide_path.read_text(encoding="utf-8")
        self.assertNotIn("release record", content.lower())


if __name__ == "__main__":
    unittest.main()
