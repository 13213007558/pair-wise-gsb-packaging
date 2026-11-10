# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

"""Offline verification of installed-package provenance.

The entry point is :func:`verify_source`. It compares an *approved
requirement* (the direct reference that the supply-chain policy allows) with
the ``direct_url`` data recorded for an installed package (see :pep:`610`),
and decides whether the recorded evidence actually proves that the installed
artifact comes from the approved source or archive.

The verification:

* never accesses the network;
* never installs, build, or import the package;
* never mutates the input objects;
* never reports credentials embedded in input URLs.

Being able to import a package on the current machine is deliberately not
treated as provenance: only immutable evidence carried by the recorded
``direct_url`` (a VCS ``commit_id`` or a trusted archive digest) can produce a
:attr:`VerificationStatus.MATCH`.
"""

from __future__ import annotations

import dataclasses
import enum
import urllib.parse
from collections.abc import Mapping
from typing import Any, TypedDict

from packaging.direct_url import DirectUrl, DirectUrlValidationError
from packaging.utils import InvalidName, canonicalize_name

__all__ = [
    "Evidence",
    "EvidenceType",
    "SourceKind",
    "SourceVerificationError",
    "VerificationResult",
    "VerificationStatus",
    "verify_source",
]


def __dir__() -> list[str]:
    return __all__


class VerificationStatus(enum.Enum):
    """Outcome of a provenance verification.

    .. versionadded:: 26.4
    """

    #: The recorded immutable evidence proves the approved source/archive.
    MATCH = "match"
    #: The recorded evidence contradicts the approved requirement.
    MISMATCH = "mismatch"
    #: The evidence is compatible but not immutable enough to prove identity.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class SourceKind(enum.Enum):
    """The kind of direct reference being compared.

    .. versionadded:: 26.4
    """

    #: A version control reference (``vcs_info``).
    VCS = "vcs"
    #: A downloaded archive (``archive_info``).
    ARCHIVE = "archive"
    #: A local directory, editable or not (``dir_info``).
    DIRECTORY = "directory"


class EvidenceType(enum.Enum):
    """The kind of evidence an :class:`Evidence` entry is based on.

    .. versionadded:: 26.4
    """

    #: Normalized project/distribution names refer to the same package.
    PROJECT_NAME = "project_name"
    #: Both sides describe the same source kind (VCS/archive/directory).
    SOURCE_KIND = "source_kind"
    #: Normalized URL identity (credentials removed, encoding canonicalized).
    URL = "url"
    #: The PEP 610 ``subdirectory`` field.
    SUBDIRECTORY = "subdirectory"
    #: The VCS system (for example ``git``).
    VCS = "vcs"
    #: The immutable VCS ``commit_id``.
    VCS_COMMIT_ID = "vcs_commit_id"
    #: The movable ``requested_revision`` (branch or tag name).
    VCS_REQUESTED_REVISION = "vcs_requested_revision"
    #: One archive digest, compared per hash algorithm.
    ARCHIVE_HASH = "archive_hash"
    #: A local directory path (never immutable by itself).
    DIRECTORY_PATH = "directory_path"
    #: Whether the local directory install was editable.
    EDITABLE = "editable"


class SourceVerificationError(Exception):
    """Raised when an input cannot be interpreted for verification.

    Error messages never contain credentials found in input URLs.

    .. versionadded:: 26.4
    """


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Evidence:
    """A single piece of evidence actually compared during verification.

    :param type: What kind of evidence was compared.
    :param matched: ``True`` if this evidence agreed, ``False`` if it
        conflicted, or ``None`` if it could not be used as immutable proof.
    :param detail: A human-readable, credential-free description.

    .. versionadded:: 26.4
    """

    type: EvidenceType
    matched: bool | None
    detail: str


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class VerificationResult:
    """The result of :func:`verify_source`.

    :param status: The overall verdict.
    :param kind: The source kind that was compared, or ``None`` when the
        inputs do not even describe the same kind of source.
    :param evidence: Every piece of evidence actually compared, in order.
    :param missing: Fields that would be needed for a stronger conclusion.
    :param reasons: Human-readable, credential-free explanations of the
        verdict.

    .. versionadded:: 26.4
    """

    status: VerificationStatus
    kind: SourceKind | None
    evidence: tuple[Evidence, ...]
    missing: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def matched(self) -> bool:
        """Whether the verdict is :attr:`VerificationStatus.MATCH`."""
        return self.status is VerificationStatus.MATCH


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class _Source:
    kind: SourceKind
    identity: tuple[Any, ...]
    redacted_url: str
    subdirectory: str | None
    vcs: str | None = None
    commit_id: str | None = None
    requested_revision: str | None = None
    hashes: dict[str, str] | None = None
    editable: bool | None = None


class _CommonFields(TypedDict):
    identity: tuple[Any, ...]
    redacted_url: str
    subdirectory: str | None


_VCS_SCHEME_PREFIXES = ("git+", "hg+", "svn+", "bzr+")


def _normalize_url(url: str) -> tuple[tuple[Any, ...], str, str | None]:
    """Return a comparable identity, a credential-free display URL, and a VCS
    hint derived from a possible ``vcs+`` URL scheme prefix."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SourceVerificationError(f"Invalid URL: {exc}") from exc
    scheme = parsed.scheme.lower()
    vcs_hint: str | None = None
    for prefix in _VCS_SCHEME_PREFIXES:
        if scheme.startswith(prefix):
            vcs_hint = prefix.removesuffix("+")
            scheme = scheme[len(prefix) :]
            break
    if not scheme:
        raise SourceVerificationError("URL is missing a scheme")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SourceVerificationError(f"Invalid URL: {exc}") from exc
    netloc = parsed.netloc
    if "@" in netloc:
        # Always discard userinfo: credentials must never appear in reports.
        netloc = netloc.rsplit("@", 1)[1]
    redacted_url = urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
    # Canonicalize percent-encoding while keeping structural characters.
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/")
    query = urllib.parse.quote(urllib.parse.unquote(parsed.query), safe="=&/?+:@,;$")
    fragment = urllib.parse.quote(urllib.parse.unquote(parsed.fragment), safe="")
    identity = (scheme, hostname, port, path, query, fragment)
    return identity, redacted_url, vcs_hint


def _normalize_subdirectory(value: str | None) -> str | None:
    if value is None:
        return None
    segments = [segment for segment in value.split("/") if segment and segment != "."]
    return "/".join(segments) or None


def _optional_str(d: Mapping[str, Any], key: str) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceVerificationError(f"{key!r} must be a string")
    return value


def _parse_requirement_hashes(
    info: Mapping[str, Any],
) -> dict[str, str] | None:
    hashes: dict[str, str] = {}
    raw_hashes = info.get("hashes")
    if raw_hashes is not None:
        if not isinstance(raw_hashes, Mapping):
            raise SourceVerificationError(
                "archive_info.hashes must be an object of algorithm to digest"
            )
        for algorithm, digest in raw_hashes.items():
            if not isinstance(algorithm, str) or not isinstance(digest, str):
                raise SourceVerificationError(
                    "archive_info.hashes entries must map strings to strings"
                )
            hashes[algorithm.lower()] = digest.lower()
    legacy_hash = info.get("hash")
    if legacy_hash is not None:
        if not isinstance(legacy_hash, str) or "=" not in legacy_hash:
            raise SourceVerificationError(
                "archive_info.hash must use the '<algorithm>=<digest>' format"
            )
        algorithm, digest = legacy_hash.split("=", 1)
        hashes.setdefault(algorithm.lower(), digest.lower())
    return hashes or None


def _source_from_direct_url(direct_url: DirectUrl) -> _Source:
    identity, redacted_url, vcs_hint = _normalize_url(direct_url.url)
    common: _CommonFields = {
        "identity": identity,
        "redacted_url": redacted_url,
        "subdirectory": _normalize_subdirectory(direct_url.subdirectory),
    }
    if direct_url.vcs_info is not None:
        info = direct_url.vcs_info
        return _Source(
            kind=SourceKind.VCS,
            vcs=info.vcs or vcs_hint,
            commit_id=info.commit_id,
            requested_revision=info.requested_revision,
            **common,
        )
    if direct_url.archive_info is not None:
        raw_hashes = direct_url.archive_info.hashes or {}
        hashes = {
            algorithm.lower(): digest.lower()
            for algorithm, digest in raw_hashes.items()
        }
        return _Source(
            kind=SourceKind.ARCHIVE,
            hashes=hashes or None,
            **common,
        )
    assert direct_url.dir_info is not None
    return _Source(
        kind=SourceKind.DIRECTORY,
        editable=direct_url.dir_info.editable,
        **common,
    )


def _source_from_requirement_dict(data: Mapping[str, Any]) -> _Source:
    url = _optional_str(data, "url")
    if url is None:
        raise SourceVerificationError("Requirement requires a string 'url'")
    identity, redacted_url, vcs_hint = _normalize_url(url)
    subdirectory = _normalize_subdirectory(_optional_str(data, "subdirectory"))
    common: _CommonFields = {
        "identity": identity,
        "redacted_url": redacted_url,
        "subdirectory": subdirectory,
    }
    sections = [
        key
        for key in ("vcs_info", "archive_info", "dir_info")
        if data.get(key) is not None
    ]
    if not sections:
        raise SourceVerificationError(
            "Requirement pins no source kind; one of vcs_info, archive_info, "
            "or dir_info is required"
        )
    if len(sections) > 1:
        raise SourceVerificationError(
            "Requirement must contain at most one of vcs_info, archive_info, "
            "or dir_info"
        )
    section = sections[0]
    info = data[section]
    if not isinstance(info, Mapping):
        raise SourceVerificationError(f"{section!r} must be an object")
    if section == "vcs_info":
        vcs = _optional_str(info, "vcs") or vcs_hint
        if vcs is None:
            raise SourceVerificationError(
                "vcs_info requires a 'vcs' value or a vcs+ URL scheme"
            )
        return _Source(
            kind=SourceKind.VCS,
            vcs=vcs.lower(),
            commit_id=_optional_str(info, "commit_id"),
            requested_revision=_optional_str(info, "requested_revision"),
            **common,
        )
    if section == "archive_info":
        return _Source(
            kind=SourceKind.ARCHIVE,
            hashes=_parse_requirement_hashes(info),
            **common,
        )
    editable = info.get("editable")
    if editable is not None and not isinstance(editable, bool):
        raise SourceVerificationError("dir_info.editable must be a boolean")
    return _Source(kind=SourceKind.DIRECTORY, editable=editable, **common)


def _coerce_source(value: object, *, strict: bool) -> _Source:
    if isinstance(value, DirectUrl):
        return _source_from_direct_url(value)
    if isinstance(value, Mapping):
        if strict:
            try:
                direct_url = DirectUrl.from_dict(value)
            except DirectUrlValidationError as exc:
                raise SourceVerificationError(str(exc)) from exc
            return _source_from_direct_url(direct_url)
        return _source_from_requirement_dict(value)
    raise SourceVerificationError(
        "Expected a DirectUrl instance or a direct_url JSON mapping"
    )


def _result(
    status: VerificationStatus,
    kind: SourceKind | None,
    evidence: list[Evidence],
    missing: list[str],
    reasons: list[str],
) -> VerificationResult:
    return VerificationResult(
        status=status,
        kind=kind,
        evidence=tuple(evidence),
        missing=tuple(missing),
        reasons=tuple(reasons),
    )


def _verify_vcs(
    requirement: _Source,
    installed: _Source,
    evidence: list[Evidence],
    missing: list[str],
    reasons: list[str],
) -> VerificationStatus:
    if requirement.vcs is not None and requirement.vcs != (installed.vcs or "").lower():
        evidence.append(
            Evidence(
                type=EvidenceType.VCS,
                matched=False,
                detail=(
                    f"approved vcs {requirement.vcs!r} != recorded vcs "
                    f"{installed.vcs!r}"
                ),
            )
        )
        reasons.append("The VCS systems do not match.")
        return VerificationStatus.MISMATCH
    evidence.append(
        Evidence(
            type=EvidenceType.VCS,
            matched=True,
            detail=f"vcs {installed.vcs!r}",
        )
    )
    if requirement.commit_id is None:
        # A branch/tag name is movable: only compare it when no immutable
        # commit_id anchors the requirement.
        required_revision = requirement.requested_revision
        if (
            required_revision is not None
            and installed.requested_revision is not None
            and required_revision != installed.requested_revision
        ):
            evidence.append(
                Evidence(
                    type=EvidenceType.VCS_REQUESTED_REVISION,
                    matched=False,
                    detail=(
                        f"approved requested_revision {required_revision!r} != "
                        f"recorded requested_revision "
                        f"{installed.requested_revision!r}"
                    ),
                )
            )
            reasons.append(
                "The requested branch/tag revisions differ and no immutable "
                "commit_id is pinned."
            )
            return VerificationStatus.MISMATCH
        if required_revision is not None and installed.requested_revision is not None:
            evidence.append(
                Evidence(
                    type=EvidenceType.VCS_REQUESTED_REVISION,
                    matched=True,
                    detail=(
                        f"requested_revision {installed.requested_revision!r} "
                        "matches, but branch/tag names are movable"
                    ),
                )
            )
        missing.append("vcs_info.commit_id")
        reasons.append(
            "The requirement does not pin an immutable commit_id; matching URL "
            "and branch/tag name cannot prove that the same revision was "
            "installed."
        )
        return VerificationStatus.INSUFFICIENT_EVIDENCE
    if installed.commit_id is None:
        missing.append("installed vcs_info.commit_id")
        reasons.append(
            "The installed record carries no commit_id to compare against the "
            "approved immutable commit."
        )
        return VerificationStatus.INSUFFICIENT_EVIDENCE
    if requirement.commit_id.lower() != installed.commit_id.lower():
        evidence.append(
            Evidence(
                type=EvidenceType.VCS_COMMIT_ID,
                matched=False,
                detail=(
                    f"approved commit_id {requirement.commit_id} != recorded "
                    f"commit_id {installed.commit_id}"
                ),
            )
        )
        reasons.append(
            "The recorded commit_id differs from the approved immutable "
            "commit, so the installed code is not the approved revision."
        )
        return VerificationStatus.MISMATCH
    evidence.append(
        Evidence(
            type=EvidenceType.VCS_COMMIT_ID,
            matched=True,
            detail=f"immutable commit_id {installed.commit_id} matches",
        )
    )
    if requirement.requested_revision is not None:
        evidence.append(
            Evidence(
                type=EvidenceType.VCS_REQUESTED_REVISION,
                matched=None,
                detail=(
                    "requested_revision is not used for the verdict; the "
                    "immutable commit_id already proves provenance"
                ),
            )
        )
    return VerificationStatus.MATCH


def _verify_archive(
    requirement: _Source,
    installed: _Source,
    evidence: list[Evidence],
    missing: list[str],
    reasons: list[str],
) -> VerificationStatus:
    required_hashes = requirement.hashes or {}
    recorded_hashes = installed.hashes or {}
    if not required_hashes:
        evidence.append(
            Evidence(
                type=EvidenceType.ARCHIVE_HASH,
                matched=None,
                detail=(
                    "the requirement pins no trusted digest; a matching URL "
                    "alone does not prove archive content"
                ),
            )
        )
        missing.append("archive_info.hashes")
        reasons.append(
            "No trusted digest constraint is pinned, so archive content "
            "cannot be proven."
        )
        return VerificationStatus.INSUFFICIENT_EVIDENCE
    has_mismatch = False
    has_gap = False
    for algorithm, expected_digest in required_hashes.items():
        recorded_digest = recorded_hashes.get(algorithm)
        if recorded_digest is None:
            evidence.append(
                Evidence(
                    type=EvidenceType.ARCHIVE_HASH,
                    matched=None,
                    detail=(
                        f"{algorithm}: required digest is not recorded for the "
                        "installed artifact"
                    ),
                )
            )
            missing.append(f"archive_info.hashes.{algorithm}")
            has_gap = True
        elif recorded_digest != expected_digest:
            evidence.append(
                Evidence(
                    type=EvidenceType.ARCHIVE_HASH,
                    matched=False,
                    detail=(
                        f"{algorithm}: recorded digest differs from the "
                        "approved digest"
                    ),
                )
            )
            has_mismatch = True
        else:
            evidence.append(
                Evidence(
                    type=EvidenceType.ARCHIVE_HASH,
                    matched=True,
                    detail=f"{algorithm}: digest matches",
                )
            )
    if has_mismatch:
        reasons.append(
            "At least one required archive digest differs; every algorithm "
            "pinned by the requirement must match."
        )
        return VerificationStatus.MISMATCH
    if has_gap:
        reasons.append(
            "Some required digest algorithms are not recorded for the "
            "installed artifact, so the full constraint cannot be verified."
        )
        return VerificationStatus.INSUFFICIENT_EVIDENCE
    return VerificationStatus.MATCH


def _verify_directory(
    requirement: _Source,
    installed: _Source,
    evidence: list[Evidence],
    missing: list[str],
    reasons: list[str],
) -> VerificationStatus:
    evidence.append(
        Evidence(
            type=EvidenceType.DIRECTORY_PATH,
            matched=True,
            detail=f"local directory path {installed.redacted_url} matches",
        )
    )
    if requirement.editable is not None and installed.editable is not None:
        editable_matches = requirement.editable is installed.editable
        evidence.append(
            Evidence(
                type=EvidenceType.EDITABLE,
                matched=editable_matches,
                detail=(
                    f"requirement editable={requirement.editable}, recorded "
                    f"editable={installed.editable}"
                ),
            )
        )
    elif installed.editable is not None:
        evidence.append(
            Evidence(
                type=EvidenceType.EDITABLE,
                matched=None,
                detail=f"recorded editable={installed.editable}",
            )
        )
    missing.append("immutable content evidence (a VCS commit_id or an archive digest)")
    if installed.editable:
        reasons.append(
            "An editable install points at a mutable local working tree; the "
            "same path can contain different content at any time, so path "
            "identity cannot prove an immutable artifact."
        )
    else:
        reasons.append(
            "A local directory path is not an immutable artifact; identical "
            "paths do not prove identical content without a digest or VCS "
            "commit."
        )
    return VerificationStatus.INSUFFICIENT_EVIDENCE


def verify_source(
    requirement: DirectUrl | Mapping[str, Any],
    installed: DirectUrl | Mapping[str, Any],
    *,
    requirement_name: str | None = None,
    installed_name: str | None = None,
) -> VerificationResult:
    """Verify installed-package provenance against an approved requirement.

    Both arguments are either :class:`~packaging.direct_url.DirectUrl`
    instances or PEP 610 ``direct_url`` JSON mappings. The *installed* mapping
    is validated strictly against the specification; the *requirement* mapping
    is accepted in a lenient form so that an approved policy may pin only the
    fields it considers immutable (for example ``vcs_info.commit_id`` without a
    ``requested_revision``).

    :param requirement: The approved direct-reference requirement.
    :param installed: The ``direct_url`` data recorded for the installed
        package.
    :param requirement_name: Optional distribution name of the requirement.
    :param installed_name: Optional distribution name of the installed
        package; when both names are given they are canonicalized before
        comparison.
    :raises SourceVerificationError: If an input is structurally invalid.
    :returns: A :class:`VerificationResult` whose ``evidence`` lists every
        evidence type actually compared.

    The comparison is purely offline: it never performs network access,
    installs, or imports, and neither input object is mutated. Credentials in
    URLs are stripped from every message and report.

    .. versionadded:: 26.4
    """
    requirement_source = _coerce_source(requirement, strict=False)
    installed_source = _coerce_source(installed, strict=True)

    if requirement_name is not None and installed_name is not None:
        try:
            canonical_requirement_name = canonicalize_name(requirement_name)
            canonical_installed_name = canonicalize_name(installed_name)
        except InvalidName as exc:
            raise SourceVerificationError(f"Invalid project name: {exc}") from exc
        if canonical_requirement_name != canonical_installed_name:
            return _result(
                VerificationStatus.MISMATCH,
                None,
                [
                    Evidence(
                        type=EvidenceType.PROJECT_NAME,
                        matched=False,
                        detail=(
                            f"approved project {canonical_requirement_name!r} "
                            f"!= installed project {canonical_installed_name!r}"
                        ),
                    )
                ],
                [],
                ["The names do not refer to the same distribution."],
            )

    if requirement_source.kind is not installed_source.kind:
        return _result(
            VerificationStatus.MISMATCH,
            None,
            [
                Evidence(
                    type=EvidenceType.SOURCE_KIND,
                    matched=False,
                    detail=(
                        f"approved kind {requirement_source.kind.value!r} != "
                        f"recorded kind {installed_source.kind.value!r}"
                    ),
                )
            ],
            [],
            ["The inputs describe different kinds of sources."],
        )

    evidence: list[Evidence] = []
    missing: list[str] = []
    reasons: list[str] = []
    kind = requirement_source.kind

    url_matches = requirement_source.identity == installed_source.identity
    evidence.append(
        Evidence(
            type=EvidenceType.URL,
            matched=url_matches,
            detail=(
                f"approved URL {requirement_source.redacted_url} vs recorded "
                f"URL {installed_source.redacted_url}"
            ),
        )
    )
    if not url_matches:
        reasons.append(
            "The normalized repository/archive URLs do not identify the same "
            "location."
        )
        return _result(VerificationStatus.MISMATCH, kind, evidence, missing, reasons)

    subdirectory_matches = (
        requirement_source.subdirectory == installed_source.subdirectory
    )
    evidence.append(
        Evidence(
            type=EvidenceType.SUBDIRECTORY,
            matched=subdirectory_matches,
            detail=(
                f"approved subdirectory {requirement_source.subdirectory!r} "
                f"vs recorded subdirectory {installed_source.subdirectory!r}"
            ),
        )
    )
    if not subdirectory_matches:
        reasons.append(
            "The subdirectory fields identify different projects within the "
            "same repository or archive."
        )
        return _result(VerificationStatus.MISMATCH, kind, evidence, missing, reasons)

    if kind is SourceKind.VCS:
        status = _verify_vcs(
            requirement_source, installed_source, evidence, missing, reasons
        )
    elif kind is SourceKind.ARCHIVE:
        status = _verify_archive(
            requirement_source, installed_source, evidence, missing, reasons
        )
    else:
        status = _verify_directory(
            requirement_source, installed_source, evidence, missing, reasons
        )
    return _result(status, kind, evidence, missing, reasons)
