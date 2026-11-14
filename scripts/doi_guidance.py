#!/usr/bin/env python3
"""DOI issuance state machine and metadata governance validator.

Enforces lifecycle rules for Digital Object Identifier (DOI) registration across
public Zenodo and private manual deposit workflows:
- States: UNSELECTED, REQUESTED, PENDING, ISSUED, UNAVAILABLE.
- Strict guard rule: A DOI is NEVER recorded in metadata or release evidence before
  issuance is verified.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import re
from typing import Any, Dict, Optional, Set, Tuple, Union

DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")


class DoiError(ValueError):
    """Raised when a DOI transition or recording rule is violated."""


class DoiState(str, Enum):
    """Canonical states for DOI registration lifecycle."""
    UNSELECTED = "unselected"
    REQUESTED = "requested"
    PENDING = "pending"
    ISSUED = "issued"
    UNAVAILABLE = "unavailable"

    @classmethod
    def from_str(cls, value: Union[str, DoiState]) -> DoiState:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise DoiError(f"Expected DOI state string or enum, got {type(value).__name__}")
        normalized = value.strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        valid = ", ".join(m.value for m in cls)
        raise DoiError(f"Invalid DOI state: {value!r}. Must be one of: {valid}")


VALID_TRANSITIONS: Dict[DoiState, Set[DoiState]] = {
    DoiState.UNSELECTED: {
        DoiState.UNSELECTED,
        DoiState.REQUESTED,
        DoiState.UNAVAILABLE,
    },
    DoiState.REQUESTED: {
        DoiState.REQUESTED,
        DoiState.PENDING,
        DoiState.UNAVAILABLE,
        DoiState.UNSELECTED,
    },
    DoiState.PENDING: {
        DoiState.PENDING,
        DoiState.ISSUED,
        DoiState.REQUESTED,
        DoiState.UNAVAILABLE,
        DoiState.UNSELECTED,
    },
    DoiState.ISSUED: {
        DoiState.ISSUED,
        DoiState.UNAVAILABLE,
    },
    DoiState.UNAVAILABLE: {
        DoiState.UNAVAILABLE,
        DoiState.REQUESTED,
        DoiState.UNSELECTED,
    },
}


def validate_doi(doi: str) -> str:
    """Validate that a string conforms to canonical DOI syntax (10.xxxx/...).

    Raises DoiError if invalid or not a bare DOI string.
    """
    if not isinstance(doi, str) or not doi.strip():
        raise DoiError("DOI string cannot be empty")
    stripped = doi.strip()
    if not DOI_PATTERN.fullmatch(stripped):
        raise DoiError(f"Invalid DOI format: {doi!r}. Expected pattern: 10.xxxx/...")
    return stripped


class DoiStateMachine:
    """State machine governing DOI registration lifecycle and guard rules."""

    def __init__(
        self,
        state: Union[DoiState, str] = DoiState.UNSELECTED,
        doi: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        canonical_state = DoiState.from_str(state)
        self._guard_doi_recording(canonical_state, doi)
        self._state = canonical_state
        self._doi = validate_doi(doi) if doi is not None and canonical_state == DoiState.ISSUED else None
        self._metadata = dict(metadata or {})

    @property
    def state(self) -> DoiState:
        return self._state

    @property
    def doi(self) -> Optional[str]:
        return self._doi

    @property
    def metadata(self) -> Dict[str, Any]:
        return dict(self._metadata)

    @staticmethod
    def _guard_doi_recording(target_state: DoiState, doi: Optional[str]) -> None:
        if target_state != DoiState.ISSUED:
            if doi is not None and str(doi).strip():
                raise DoiError(
                    f"A DOI string cannot be recorded when state is '{target_state.value}'. "
                    f"A DOI is recorded ONLY after issuance is verified in '{DoiState.ISSUED.value}' state."
                )
        else:
            if doi is None or not str(doi).strip():
                raise DoiError(
                    f"State '{DoiState.ISSUED.value}' requires a verified DOI string matching pattern {DOI_PATTERN.pattern}"
                )

    def can_transition_to(self, target_state: Union[DoiState, str]) -> bool:
        canonical_target = DoiState.from_str(target_state)
        return canonical_target in VALID_TRANSITIONS.get(self._state, set())

    def transition(
        self,
        target_state: Union[DoiState, str],
        doi: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DoiStateMachine:
        canonical_target = DoiState.from_str(target_state)
        if not self.can_transition_to(canonical_target):
            raise DoiError(
                f"Invalid transition from '{self._state.value}' to '{canonical_target.value}'. "
                f"Allowed transitions from '{self._state.value}': "
                f"{', '.join(s.value for s in sorted(VALID_TRANSITIONS.get(self._state, set()), key=lambda s: s.value))}"
            )
        if self._state == DoiState.ISSUED and canonical_target == DoiState.ISSUED and doi is None:
            effective_doi = self._doi
        else:
            effective_doi = doi
        merged_metadata = dict(self._metadata)
        if metadata:
            merged_metadata.update(metadata)
        return DoiStateMachine(canonical_target, doi=effective_doi, metadata=merged_metadata)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "state": self._state.value,
            "doi": self._doi,
        }
        if self._metadata:
            result["metadata"] = dict(self._metadata)
        return result


def parse_cff_scalars(text: str) -> Dict[str, str]:
    """Parse top-level scalar keys from CITATION.cff text without YAML dependencies."""
    keys: Dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*?)[ \t]*$", line)
        if match:
            value = match.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            keys[match.group(1)] = value
    return keys


def parse_cff_metadata(content: Union[str, Path]) -> Tuple[str, Dict[str, str]]:
    """Parse CITATION.cff content (from string or file Path), returning (text, keys)."""
    if isinstance(content, Path):
        text = content.read_text(encoding="utf-8")
    else:
        text = str(content)
    return text, parse_cff_scalars(text)


_parse_cff_text = parse_cff_metadata


def validate_citation_cff_doi(
    citation_content: Union[str, Path],
    doi_state: Union[DoiState, str],
    expected_doi: Optional[str] = None,
) -> Optional[str]:
    """Validate that CITATION.cff adheres to DOI recording governance.

    - When doi_state is not ISSUED: CITATION.cff MUST NOT contain a 'doi' field.
      If an unverified DOI is present, raises DoiError.
    - When doi_state is ISSUED: CITATION.cff MUST contain a well-formed 'doi' field.
      If expected_doi is supplied, it must match exactly.
    """
    state = DoiState.from_str(doi_state)
    _, keys = _parse_cff_text(citation_content)
    declared_doi = keys.get("doi")

    if state != DoiState.ISSUED:
        if declared_doi is not None and declared_doi.strip():
            raise DoiError(
                f"CITATION.cff declares unverified DOI {declared_doi!r} while DOI state is '{state.value}'. "
                "A DOI is never recorded in citation metadata before issuance is verified."
            )
        return None

    if declared_doi is None or not declared_doi.strip():
        raise DoiError("DOI state is 'issued' but CITATION.cff is missing required 'doi' field")

    validated = validate_doi(declared_doi)
    if expected_doi is not None:
        canonical_expected = validate_doi(expected_doi)
        if validated != canonical_expected:
            raise DoiError(
                f"CITATION.cff DOI {validated!r} does not match expected verified DOI {canonical_expected!r}"
            )
    return validated


def update_citation_cff_doi(
    citation_text: str,
    doi_state: Union[DoiState, str],
    doi: Optional[str] = None,
) -> str:
    """Update or strip the 'doi:' entry in CITATION.cff according to DOI state.

    - When state is ISSUED: ensures 'doi: <verified_doi>' is present.
    - When state is not ISSUED: removes any 'doi:' declaration to prevent unverified recording.
    """
    state = DoiState.from_str(doi_state)
    lines = citation_text.splitlines()
    filtered = [line for line in lines if not re.match(r"^doi:[ \t]*", line)]

    if state != DoiState.ISSUED:
        return "\n".join(filtered) + ("\n" if citation_text.endswith("\n") else "")

    validated_doi = validate_doi(doi if doi is not None else "")
    insert_idx = None
    for idx, line in enumerate(filtered):
        if re.match(r"^(date-released|version):", line):
            insert_idx = idx + 1
        elif re.match(r"^authors:", line) and insert_idx is None:
            insert_idx = idx

    doi_line = f"doi: {validated_doi}"
    if insert_idx is not None:
        filtered.insert(insert_idx, doi_line)
    else:
        filtered.append(doi_line)

    return "\n".join(filtered) + ("\n" if citation_text.endswith("\n") else "")


def validate_release_doi_metadata(release_dict: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Validate release evidence dictionary conforms to DOI governance rules.

    Returns tuple (state_value, verified_doi_or_none).
    """
    if not isinstance(release_dict, dict):
        raise DoiError(f"Expected release metadata dictionary, got {type(release_dict).__name__}")
    raw_state = release_dict.get("doi_state", DoiState.UNSELECTED.value)
    state = DoiState.from_str(raw_state)
    doi = release_dict.get("doi")

    if state != DoiState.ISSUED:
        if doi is not None and str(doi).strip():
            raise DoiError(
                f"A DOI string cannot be recorded in release metadata when state is '{state.value}'. "
                f"Observed DOI: {doi!r}"
            )
        return (state.value, None)

    if doi is None or not str(doi).strip():
        raise DoiError(
            f"Release metadata with state '{state.value}' requires a verified DOI string"
        )
    return (state.value, validate_doi(doi))


def format_doi_release_notes(doi_state: Union[DoiState, str], doi: Optional[str] = None) -> str:
    """Format release notes section according to verified DOI state."""
    state = DoiState.from_str(doi_state)
    if state == DoiState.UNSELECTED:
        return "DOI registration: unselected (this release does not register a DOI)."
    if state == DoiState.REQUESTED:
        return "DOI registration: requested (deposit registration initiated; no DOI is recorded prior to verification)."
    if state == DoiState.PENDING:
        return "DOI registration: pending verification (deposit submitted; DOI string will be recorded once minted and verified)."
    if state == DoiState.ISSUED:
        verified_doi = validate_doi(doi if doi is not None else "")
        return f"DOI: `{verified_doi}` (verified release archive: https://doi.org/{verified_doi})."
    if state == DoiState.UNAVAILABLE:
        return "DOI registration: unavailable (deposit was rejected, retracted, or service is unavailable)."
    return f"DOI registration: {state.value}."
