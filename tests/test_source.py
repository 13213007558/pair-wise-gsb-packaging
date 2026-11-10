# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import annotations

import copy

import pytest

from packaging.direct_url import DirectUrl
from packaging.source import (
    Evidence,
    EvidenceType,
    SourceKind,
    SourceVerificationError,
    VerificationResult,
    VerificationStatus,
    verify_source,
)

VCS_URL = "https://git.example.org/owner/repo.git"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
DIGEST_A = "f" * 64
DIGEST_B = "e" * 64


def vcs_requirement(
    *,
    url: str = VCS_URL,
    commit_id: str | None = COMMIT_A,
    requested_revision: str | None = "main",
    vcs: str | None = "git",
    subdirectory: str | None = None,
) -> dict[str, object]:
    info: dict[str, object] = {}
    if vcs is not None:
        info["vcs"] = vcs
    if commit_id is not None:
        info["commit_id"] = commit_id
    if requested_revision is not None:
        info["requested_revision"] = requested_revision
    result: dict[str, object] = {"url": url, "vcs_info": info}
    if subdirectory is not None:
        result["subdirectory"] = subdirectory
    return result


def vcs_installed(
    *,
    url: str = VCS_URL,
    commit_id: str = COMMIT_A,
    requested_revision: str | None = "main",
    subdirectory: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "url": url,
        "vcs_info": {
            "vcs": "git",
            "commit_id": commit_id,
            "requested_revision": requested_revision,
        },
    }
    if subdirectory is not None:
        result["subdirectory"] = subdirectory
    return result


def archive_requirement(
    *, hashes: dict[str, str] | None = None, legacy_hash: str | None = None
) -> dict[str, object]:
    info: dict[str, object] = {}
    if hashes is not None:
        info["hashes"] = hashes
    if legacy_hash is not None:
        info["hash"] = legacy_hash
    return {"url": "https://files.example.org/pkg-1.0.tar.gz", "archive_info": info}


def archive_installed(*, hashes: dict[str, str]) -> dict[str, object]:
    return {
        "url": "https://files.example.org/pkg-1.0.tar.gz",
        "archive_info": {"hashes": hashes},
    }


def directory_source(*, editable: bool) -> dict[str, object]:
    return {
        "url": "file:///srv/local/pkg",
        "dir_info": {"editable": editable},
    }


def evidence_types(result: VerificationResult) -> list[EvidenceType]:
    return [entry.type for entry in result.evidence]


# --- VCS -------------------------------------------------------------------


def test_pinned_commit_matches_immutable_evidence() -> None:
    result = verify_source(vcs_requirement(), vcs_installed())
    assert result.status is VerificationStatus.MATCH
    assert result.matched is True
    assert result.kind is SourceKind.VCS
    assert result.missing == ()
    assert EvidenceType.VCS_COMMIT_ID in evidence_types(result)
    commit_evidence = next(
        entry for entry in result.evidence if entry.type is EvidenceType.VCS_COMMIT_ID
    )
    assert commit_evidence.matched is True
    # The movable requested_revision is reported but cannot overturn a commit.
    revision_evidence = next(
        entry
        for entry in result.evidence
        if entry.type is EvidenceType.VCS_REQUESTED_REVISION
    )
    assert revision_evidence.matched is None


def test_commit_id_match_is_case_insensitive() -> None:
    installed = vcs_installed(commit_id=COMMIT_A.upper())
    result = verify_source(vcs_requirement(commit_id=COMMIT_A), installed)
    assert result.status is VerificationStatus.MATCH


def test_same_branch_but_different_commit_is_mismatch() -> None:
    installed = vcs_installed(commit_id=COMMIT_B, requested_revision="main")
    result = verify_source(vcs_requirement(requested_revision="main"), installed)
    assert result.status is VerificationStatus.MISMATCH
    commit_evidence = next(
        entry for entry in result.evidence if entry.type is EvidenceType.VCS_COMMIT_ID
    )
    assert commit_evidence.matched is False


def test_url_and_branch_alone_cannot_provenance_match() -> None:
    requirement = vcs_requirement(commit_id=None, requested_revision="main")
    installed = vcs_installed(commit_id=COMMIT_B, requested_revision="main")
    result = verify_source(requirement, installed)
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert "vcs_info.commit_id" in result.missing
    assert EvidenceType.VCS_REQUESTED_REVISION in evidence_types(result)
    assert EvidenceType.VCS_COMMIT_ID not in evidence_types(result)


def test_movable_revision_differs_without_pinned_commit_is_mismatch() -> None:
    requirement = vcs_requirement(commit_id=None, requested_revision="main")
    installed = vcs_installed(commit_id=COMMIT_B, requested_revision="dev")
    result = verify_source(requirement, installed)
    assert result.status is VerificationStatus.MISMATCH
    revision_evidence = next(
        entry
        for entry in result.evidence
        if entry.type is EvidenceType.VCS_REQUESTED_REVISION
    )
    assert revision_evidence.matched is False


def test_different_repository_url_is_mismatch() -> None:
    installed = vcs_installed(url="https://git.example.org/other/repo.git")
    result = verify_source(vcs_requirement(), installed)
    assert result.status is VerificationStatus.MISMATCH
    url_evidence = next(
        entry for entry in result.evidence if entry.type is EvidenceType.URL
    )
    assert url_evidence.matched is False


def test_different_subdirectory_is_mismatch() -> None:
    requirement = vcs_requirement(subdirectory="backend")
    installed = vcs_installed(subdirectory="frontend")
    result = verify_source(requirement, installed)
    assert result.status is VerificationStatus.MISMATCH
    subdir_evidence = next(
        entry for entry in result.evidence if entry.type is EvidenceType.SUBDIRECTORY
    )
    assert subdir_evidence.matched is False


def test_vcs_scheme_prefix_and_percent_encoding_normalized() -> None:
    requirement = vcs_requirement(url="https://git.example.org/owner/r%20name.git")
    installed = vcs_installed(url="git+https://git.example.org/owner/r%20name.git")
    result = verify_source(requirement, installed)
    assert result.status is VerificationStatus.MATCH


def test_vcs_system_mismatch_is_rejected() -> None:
    requirement = vcs_requirement(vcs="hg")
    result = verify_source(requirement, vcs_installed())
    assert result.status is VerificationStatus.MISMATCH
    assert EvidenceType.VCS in evidence_types(result)


def test_credentials_are_redacted_but_url_still_matches() -> None:
    installed = vcs_installed(url="https://token:secret@git.example.org/owner/repo.git")
    result = verify_source(vcs_requirement(), installed)
    assert result.status is VerificationStatus.MATCH
    rendered = str(result)
    assert "secret" not in rendered
    assert "token" not in rendered


# --- Archive ---------------------------------------------------------------


def test_matching_archive_digest_is_match() -> None:
    result = verify_source(
        archive_requirement(hashes={"sha256": DIGEST_A}),
        archive_installed(hashes={"sha256": DIGEST_A}),
    )
    assert result.status is VerificationStatus.MATCH
    hash_evidence = next(
        entry for entry in result.evidence if entry.type is EvidenceType.ARCHIVE_HASH
    )
    assert hash_evidence.matched is True


def test_correct_url_but_different_digest_is_mismatch() -> None:
    result = verify_source(
        archive_requirement(hashes={"sha256": DIGEST_A}),
        archive_installed(hashes={"sha256": DIGEST_B}),
    )
    assert result.status is VerificationStatus.MISMATCH
    hash_evidence = next(
        entry for entry in result.evidence if entry.type is EvidenceType.ARCHIVE_HASH
    )
    assert hash_evidence.matched is False


def test_every_required_algorithm_must_match() -> None:
    requirement = archive_requirement(hashes={"sha256": DIGEST_A, "sha512": "9" * 128})
    installed = archive_installed(hashes={"sha256": DIGEST_A, "sha512": "8" * 128})
    result = verify_source(requirement, installed)
    assert result.status is VerificationStatus.MISMATCH
    compared = {
        entry.detail.split(":", 1)[0]
        for entry in result.evidence
        if entry.type is EvidenceType.ARCHIVE_HASH
    }
    assert compared == {"sha256", "sha512"}


def test_missing_required_algorithm_is_insufficient() -> None:
    requirement = archive_requirement(hashes={"sha256": DIGEST_A, "sha512": "9" * 128})
    installed = archive_installed(hashes={"sha256": DIGEST_A})
    result = verify_source(requirement, installed)
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert "archive_info.hashes.sha512" in result.missing


def test_requirement_without_digests_is_insufficient() -> None:
    result = verify_source(
        archive_requirement(),
        archive_installed(hashes={"sha256": DIGEST_A}),
    )
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert "archive_info.hashes" in result.missing


def test_legacy_hash_field_is_supported() -> None:
    result = verify_source(
        archive_requirement(legacy_hash=f"sha256={DIGEST_A}"),
        archive_installed(hashes={"sha256": DIGEST_A}),
    )
    assert result.status is VerificationStatus.MATCH


def test_archive_url_mismatch_is_mismatch() -> None:
    installed = {
        "url": "https://files.example.org/other.tar.gz",
        "archive_info": {"hashes": {"sha256": DIGEST_A}},
    }
    result = verify_source(archive_requirement(hashes={"sha256": DIGEST_A}), installed)
    assert result.status is VerificationStatus.MISMATCH


# --- Directory / editable --------------------------------------------------


def test_editable_directory_is_never_immutable_match() -> None:
    result = verify_source(
        directory_source(editable=True), directory_source(editable=True)
    )
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert EvidenceType.DIRECTORY_PATH in evidence_types(result)
    assert EvidenceType.EDITABLE in evidence_types(result)
    assert result.missing


def test_non_editable_directory_is_also_insufficient() -> None:
    result = verify_source(
        directory_source(editable=False), directory_source(editable=False)
    )
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE


def test_editable_flag_mismatch_is_recorded_but_still_insufficient() -> None:
    result = verify_source(
        directory_source(editable=False), directory_source(editable=True)
    )
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    editable_evidence = next(
        entry for entry in result.evidence if entry.type is EvidenceType.EDITABLE
    )
    assert editable_evidence.matched is False


# --- Cross-cutting ---------------------------------------------------------


def test_project_name_normalization() -> None:
    requirement = vcs_requirement()
    installed = vcs_installed()
    match = verify_source(
        requirement,
        installed,
        requirement_name="foo.bar",
        installed_name="Foo_Bar",
    )
    assert match.status is VerificationStatus.MATCH
    mismatch = verify_source(
        requirement,
        installed,
        requirement_name="foo",
        installed_name="bar",
    )
    assert mismatch.status is VerificationStatus.MISMATCH
    assert mismatch.kind is None
    assert evidence_types(mismatch) == [EvidenceType.PROJECT_NAME]


@pytest.mark.parametrize(
    ("requirement_subdir", "installed_subdir"),
    [("./sub", "sub/"), ("sub//deep", "sub/deep"), (None, None)],
)
def test_subdirectory_normalization(
    requirement_subdir: str | None, installed_subdir: str | None
) -> None:
    result = verify_source(
        vcs_requirement(subdirectory=requirement_subdir),
        vcs_installed(subdirectory=installed_subdir),
    )
    assert result.status is VerificationStatus.MATCH


def test_source_kind_mismatch() -> None:
    installed = archive_installed(hashes={"sha256": DIGEST_A})
    result = verify_source(vcs_requirement(), installed)
    assert result.status is VerificationStatus.MISMATCH
    assert result.kind is None
    assert evidence_types(result) == [EvidenceType.SOURCE_KIND]


def test_direct_url_instances_are_accepted() -> None:
    requirement = DirectUrl.from_dict(vcs_requirement())
    installed = DirectUrl.from_dict(vcs_installed())
    result = verify_source(requirement, installed)
    assert result.status is VerificationStatus.MATCH


def test_inputs_are_not_mutated() -> None:
    requirement = vcs_requirement()
    installed = vcs_installed(url="https://token:secret@git.example.org/owner/repo.git")
    requirement_snapshot = copy.deepcopy(requirement)
    installed_snapshot = copy.deepcopy(installed)
    verify_source(requirement, installed)
    assert requirement == requirement_snapshot
    assert installed == installed_snapshot


def test_invalid_installed_mapping_raises() -> None:
    with pytest.raises(SourceVerificationError):
        verify_source(
            vcs_requirement(),
            {"url": VCS_URL},
        )


def test_requirement_without_any_info_section_raises() -> None:
    with pytest.raises(SourceVerificationError):
        verify_source({"url": VCS_URL}, vcs_installed())


def test_unsupported_input_type_raises() -> None:
    with pytest.raises(SourceVerificationError):
        verify_source(vcs_requirement(), object())  # type: ignore[arg-type]


def test_vcs_derived_from_url_scheme_prefix() -> None:
    requirement = {
        "url": "git+https://git.example.org/owner/repo.git",
        "vcs_info": {"commit_id": COMMIT_A},
    }
    result = verify_source(requirement, vcs_installed())
    assert result.status is VerificationStatus.MATCH


def test_requirement_hash_algorithm_and_digest_case_insensitive() -> None:
    result = verify_source(
        archive_requirement(hashes={"SHA256": DIGEST_A.upper()}),
        archive_installed(hashes={"sha256": DIGEST_A}),
    )
    assert result.status is VerificationStatus.MATCH


def test_empty_subdirectory_equivalent_to_absent() -> None:
    requirement = vcs_requirement()
    requirement["subdirectory"] = ""
    result = verify_source(requirement, vcs_installed())
    assert result.status is VerificationStatus.MATCH


def test_requirement_without_scheme_raises() -> None:
    with pytest.raises(SourceVerificationError):
        verify_source(
            {"url": "not-a-url", "vcs_info": {"vcs": "git", "commit_id": COMMIT_A}},
            vcs_installed(),
        )


def test_requirement_with_multiple_info_sections_raises() -> None:
    requirement = vcs_requirement()
    requirement["dir_info"] = {}
    with pytest.raises(SourceVerificationError):
        verify_source(requirement, vcs_installed())


def test_malformed_legacy_hash_raises() -> None:
    with pytest.raises(SourceVerificationError):
        verify_source(
            archive_requirement(legacy_hash="no-delimiter"),
            archive_installed(hashes={"sha256": DIGEST_A}),
        )


def test_evidence_entries_are_auditable() -> None:
    result = verify_source(vcs_requirement(), vcs_installed())
    assert result.evidence
    for entry in result.evidence:
        assert isinstance(entry, Evidence)
        assert isinstance(entry.type, EvidenceType)
        assert entry.detail
