# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import posixpath
import re
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from typing import TYPE_CHECKING, BinaryIO

from .tags import Tag, parse_tag
from .utils import (
    InvalidWheelFilename,
    NormalizedName,
    canonicalize_name,
    parse_wheel_filename,
)
from .version import InvalidVersion, Version

if TYPE_CHECKING:
    from email.message import Message

__all__ = [
    "WheelIssue",
    "validate_wheel",
]


def __dir__() -> list[str]:
    return __all__


DEFAULT_MAX_MEMBERS = 100_000
"""Default limit on the number of members accepted in a wheel archive."""

DEFAULT_MAX_UNPACKED_BYTES = 1 << 30  # 1 GiB
"""Default limit on the total uncompressed size read from a wheel archive."""

_CHUNK_SIZE = 1 << 20  # 1 MiB

_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class WheelIssue:
    """
    A single problem found while validating a wheel archive.

    .. versionadded:: 26.4
    """

    kind: str
    """A machine-readable, kebab-case identifier for the kind of problem."""

    message: str
    """A human-readable description of the problem."""

    member: str | None = None
    """The archive member the problem relates to, if any."""


class _LimitExceeded(Exception):
    pass


class _Budget:
    """Tracks how many uncompressed bytes have been read from an archive."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def consume(self, amount: int) -> None:
        self.used += amount
        if self.used > self.limit:
            raise _LimitExceeded


def _normalize_member_name(name: str) -> str:
    # ZIP members use "/" as the separator, but normalize "\" as well so
    # that members which would collide once extracted are reported.
    normalized = posixpath.normpath(name.replace("\\", "/"))
    return "" if normalized == "." else normalized


def _is_unsafe_member_name(name: str) -> bool:
    normalized = _normalize_member_name(name)
    return bool(
        _DRIVE_LETTER_RE.match(name)
        or normalized == ".."
        or normalized.startswith(("/", "../"))
    )


_READ_ERRORS = (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile)


def _read_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _Budget
) -> bytes:
    chunks: list[bytes] = []
    with archive.open(info) as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            budget.consume(len(chunk))
            chunks.append(chunk)
    return b"".join(chunks)


def _read_member_checked(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    budget: _Budget,
    issues: list[WheelIssue],
    member: str,
) -> bytes | None:
    try:
        return _read_member(archive, info, budget)
    except _READ_ERRORS as exc:
        issues.append(
            WheelIssue("member-read-error", f"cannot read {member!r}: {exc}", member)
        )
        return None


def _hash_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, algorithm: str, budget: _Budget
) -> tuple[str, int]:
    hasher = hashlib.new(algorithm)
    total = 0
    with archive.open(info) as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            budget.consume(len(chunk))
            hasher.update(chunk)
            total += len(chunk)
    digest = base64.urlsafe_b64encode(hasher.digest()).rstrip(b"=").decode("ascii")
    return digest, total


def _parse_message(data: bytes) -> Message:
    return Parser().parsestr(data.decode("utf-8", errors="replace"))


def _version_matches(candidate: str, expected: Version) -> bool:
    try:
        return Version(candidate) == expected
    except InvalidVersion:
        return candidate == str(expected)


def validate_wheel(
    source: str | os.PathLike[str] | BinaryIO,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_BYTES,
) -> list[WheelIssue]:
    """
    Validate a wheel archive without installing it or importing its code.

    The archive is checked read-only: members are streamed, never extracted
    to disk. The checks cover the wheel filename, the uniqueness of the
    ``.dist-info`` directory, the ``METADATA`` name/version, the ``WHEEL``
    tags, and every ``RECORD`` entry (path, digest, and size). Duplicate ZIP
    members, path collisions after normalization, and directory traversal
    attempts are reported as well.

    .. versionadded:: 26.4

    :param source: A path to the wheel file, or a seekable binary file
        object. Network access and installation are out of scope.
    :param max_members: Maximum number of archive members accepted before
        giving up, as a guard against hostile archives.
    :param max_unpacked_bytes: Maximum total number of uncompressed bytes
        read from the archive before giving up.
    :return: A list of independent :class:`WheelIssue` objects; an empty
        list means the wheel passed every check.
    :raises ValueError: If **source** is a file object that is not seekable.
    """
    issues: list[WheelIssue] = []

    filename: str | None
    if isinstance(source, (str, os.PathLike)):
        filename = os.path.basename(os.fspath(source))
    else:
        if not source.seekable():
            raise ValueError("validate_wheel() requires a seekable file object")
        raw_name = getattr(source, "name", None)
        filename = os.path.basename(raw_name) if isinstance(raw_name, str) else None

    expected_name: NormalizedName | None = None
    expected_version: Version | None = None
    expected_tags: frozenset[Tag] | None = None
    if filename is not None:
        try:
            expected_name, expected_version, _build, expected_tags = (
                parse_wheel_filename(filename)
            )
        except InvalidWheelFilename as exc:
            issues.append(
                WheelIssue(
                    "invalid-filename", f"invalid wheel filename: {exc}", filename
                )
            )

    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        issues.append(
            WheelIssue(
                "invalid-archive", f"not a readable ZIP archive: {exc}", filename
            )
        )
        return issues

    budget = _Budget(max_unpacked_bytes)
    with archive:
        try:
            _validate_archive(
                archive,
                issues,
                expected_name,
                expected_version,
                expected_tags,
                max_members,
                budget,
            )
        except _LimitExceeded:
            issues.append(
                WheelIssue(
                    "limit-exceeded",
                    "uncompressed data exceeds the limit of"
                    f" {max_unpacked_bytes} bytes",
                )
            )
    return issues


def _validate_archive(
    archive: zipfile.ZipFile,
    issues: list[WheelIssue],
    expected_name: NormalizedName | None,
    expected_version: Version | None,
    expected_tags: frozenset[Tag] | None,
    max_members: int,
    budget: _Budget,
) -> None:
    infos = archive.infolist()
    if len(infos) > max_members:
        issues.append(
            WheelIssue(
                "limit-exceeded",
                f"archive has {len(infos)} members, more than the limit"
                f" of {max_members}",
            )
        )
        return

    # Cheap up-front check using the sizes declared in the central directory;
    # the streaming reads below re-check the actual decompressed byte count.
    declared_size = sum(info.file_size for info in infos)
    if declared_size > budget.limit:
        issues.append(
            WheelIssue(
                "limit-exceeded",
                f"declared uncompressed size of {declared_size} bytes exceeds"
                f" the limit of {budget.limit} bytes",
            )
        )
        return

    by_normalized: dict[str, list[zipfile.ZipInfo]] = {}
    normalized_owner: dict[str, str] = {}
    seen_raw: set[str] = set()
    file_members: list[str] = []

    for info in infos:
        name = info.filename
        if name in seen_raw:
            issues.append(
                WheelIssue("duplicate-member", f"duplicate ZIP member {name!r}", name)
            )
        seen_raw.add(name)

        if _is_unsafe_member_name(name):
            issues.append(
                WheelIssue(
                    "directory-traversal",
                    f"member path {name!r} escapes the installation root",
                    name,
                )
            )
            continue

        normalized = _normalize_member_name(name)
        owner = normalized_owner.get(normalized)
        if owner is not None and owner != name:
            issues.append(
                WheelIssue(
                    "path-collision",
                    f"members {owner!r} and {name!r} normalize to the same path"
                    f" {normalized!r}",
                    name,
                )
            )
        else:
            normalized_owner[normalized] = name

        by_normalized.setdefault(normalized, []).append(info)
        if not name.endswith("/"):
            file_members.append(normalized)

    dist_infos = sorted(
        {
            normalized.split("/", 1)[0]
            for normalized in by_normalized
            if normalized.split("/", 1)[0].endswith(".dist-info")
        }
    )
    if not dist_infos:
        issues.append(
            WheelIssue("missing-dist-info", "no .dist-info directory in the archive")
        )
        return
    if len(dist_infos) > 1:
        issues.append(
            WheelIssue(
                "multiple-dist-info",
                f"multiple .dist-info directories: {', '.join(dist_infos)}",
            )
        )
        return
    dist_info = dist_infos[0]

    _check_dist_info_name(dist_info, issues, expected_name, expected_version)
    _check_metadata(
        archive,
        dist_info,
        by_normalized,
        issues,
        expected_name,
        expected_version,
        budget,
    )
    _check_wheel_file(archive, dist_info, by_normalized, issues, expected_tags, budget)
    _check_record(archive, dist_info, by_normalized, file_members, issues, budget)


def _check_dist_info_name(
    dist_info: str,
    issues: list[WheelIssue],
    expected_name: NormalizedName | None,
    expected_version: Version | None,
) -> None:
    if expected_name is None or expected_version is None:
        return
    stem = dist_info[: -len(".dist-info")]
    dir_name, sep, dir_version = stem.rpartition("-")
    if not sep:
        issues.append(
            WheelIssue(
                "dist-info-name-mismatch",
                f".dist-info directory {dist_info!r} is not named"
                f" {expected_name}-{expected_version}.dist-info",
                dist_info,
            )
        )
        return
    if canonicalize_name(dir_name) != expected_name:
        issues.append(
            WheelIssue(
                "dist-info-name-mismatch",
                f".dist-info directory name {dir_name!r} does not match filename"
                f" distribution {expected_name!r}",
                dist_info,
            )
        )
    if not _version_matches(dir_version, expected_version):
        issues.append(
            WheelIssue(
                "dist-info-version-mismatch",
                f".dist-info directory version {dir_version!r} does not match"
                f" filename version {expected_version}",
                dist_info,
            )
        )


def _check_metadata(
    archive: zipfile.ZipFile,
    dist_info: str,
    by_normalized: dict[str, list[zipfile.ZipInfo]],
    issues: list[WheelIssue],
    expected_name: NormalizedName | None,
    expected_version: Version | None,
    budget: _Budget,
) -> None:
    metadata_path = f"{dist_info}/METADATA"
    metadata_infos = by_normalized.get(metadata_path)
    if not metadata_infos:
        issues.append(
            WheelIssue("missing-metadata", f"{metadata_path} is missing", metadata_path)
        )
        return
    metadata_data = _read_member_checked(
        archive, metadata_infos[0], budget, issues, metadata_path
    )
    if metadata_data is None:
        return
    message = _parse_message(metadata_data)

    dist_name = message.get("Name")
    if dist_name is None:
        issues.append(
            WheelIssue(
                "metadata-name-missing",
                f"{metadata_path} has no Name field",
                metadata_path,
            )
        )
    elif expected_name is not None and canonicalize_name(dist_name) != expected_name:
        issues.append(
            WheelIssue(
                "metadata-name-mismatch",
                f"METADATA Name {dist_name!r} does not match filename distribution"
                f" {expected_name!r}",
                metadata_path,
            )
        )

    dist_version = message.get("Version")
    if dist_version is None:
        issues.append(
            WheelIssue(
                "metadata-version-missing",
                f"{metadata_path} has no Version field",
                metadata_path,
            )
        )
    elif expected_version is not None:
        try:
            version_matches = Version(dist_version) == expected_version
        except InvalidVersion:
            issues.append(
                WheelIssue(
                    "metadata-version-invalid",
                    f"METADATA Version {dist_version!r} is not a valid version",
                    metadata_path,
                )
            )
        else:
            if not version_matches:
                issues.append(
                    WheelIssue(
                        "metadata-version-mismatch",
                        f"METADATA Version {dist_version!r} does not match filename"
                        f" version {expected_version}",
                        metadata_path,
                    )
                )


def _check_wheel_file(
    archive: zipfile.ZipFile,
    dist_info: str,
    by_normalized: dict[str, list[zipfile.ZipInfo]],
    issues: list[WheelIssue],
    expected_tags: frozenset[Tag] | None,
    budget: _Budget,
) -> None:
    wheel_path = f"{dist_info}/WHEEL"
    wheel_infos = by_normalized.get(wheel_path)
    if not wheel_infos:
        issues.append(
            WheelIssue("missing-wheel", f"{wheel_path} is missing", wheel_path)
        )
        return
    wheel_data = _read_member_checked(
        archive, wheel_infos[0], budget, issues, wheel_path
    )
    if wheel_data is None:
        return
    message = _parse_message(wheel_data)

    if message.get("Wheel-Version") is None:
        issues.append(
            WheelIssue(
                "wheel-version-missing",
                f"{wheel_path} has no Wheel-Version field",
                wheel_path,
            )
        )

    wheel_tags: set[Tag] = set()
    for header in message.get_all("Tag", []):
        wheel_tags.update(_parse_tag_header(header, issues, wheel_path))
    if not wheel_tags:
        issues.append(
            WheelIssue(
                "wheel-tags-missing", f"{wheel_path} has no Tag fields", wheel_path
            )
        )
    elif expected_tags is not None and wheel_tags != set(expected_tags):
        missing = sorted(str(tag) for tag in expected_tags - wheel_tags)
        unexpected = sorted(str(tag) for tag in wheel_tags - expected_tags)
        issues.append(
            WheelIssue(
                "wheel-tags-mismatch",
                f"WHEEL tags do not match filename tags (missing: {missing},"
                f" unexpected: {unexpected})",
                wheel_path,
            )
        )


def _parse_tag_header(
    header: str, issues: list[WheelIssue], wheel_path: str
) -> frozenset[Tag]:
    try:
        return parse_tag(header)
    except ValueError as exc:
        issues.append(
            WheelIssue(
                "wheel-tag-invalid",
                f"invalid Tag {header!r} in WHEEL: {exc}",
                wheel_path,
            )
        )
        return frozenset()


def _check_record(
    archive: zipfile.ZipFile,
    dist_info: str,
    by_normalized: dict[str, list[zipfile.ZipInfo]],
    file_members: list[str],
    issues: list[WheelIssue],
    budget: _Budget,
) -> None:
    record_path = f"{dist_info}/RECORD"
    record_infos = by_normalized.get(record_path)
    if not record_infos:
        issues.append(
            WheelIssue("missing-record", f"{record_path} is missing", record_path)
        )
        return

    # RECORD itself and its signatures are recorded with empty digest/size.
    special = {record_path, f"{record_path}.jws", f"{record_path}.p7s"}

    record_data = _read_member_checked(
        archive, record_infos[0], budget, issues, record_path
    )
    if record_data is None:
        return
    rows = csv.reader(io.StringIO(record_data.decode("utf-8", errors="replace")))

    recorded: set[str] = set()
    for row in rows:
        if not row:
            continue
        path = row[0]
        hash_spec = row[1] if len(row) > 1 else ""
        size_text = row[2] if len(row) > 2 else ""

        if _is_unsafe_member_name(path):
            issues.append(
                WheelIssue(
                    "directory-traversal",
                    f"RECORD entry {path!r} escapes the installation root",
                    path,
                )
            )
            continue
        normalized = _normalize_member_name(path)
        recorded.add(normalized)

        if normalized in special:
            if hash_spec or size_text:
                issues.append(
                    WheelIssue(
                        "record-special-entry",
                        f"RECORD entry for {normalized!r} must have an empty"
                        " digest and size",
                        normalized,
                    )
                )
            continue

        targets = by_normalized.get(normalized)
        if not targets:
            issues.append(
                WheelIssue(
                    "record-missing-member",
                    f"RECORD lists {normalized!r} which is not in the archive",
                    normalized,
                )
            )
            continue

        algorithm, sep, expected_digest = hash_spec.partition("=")
        if not sep or not algorithm or not expected_digest:
            issues.append(
                WheelIssue(
                    "record-digest-missing",
                    f"RECORD entry for {normalized!r} has no usable digest",
                    normalized,
                )
            )
            continue
        try:
            hashlib.new(algorithm)
        except ValueError:
            issues.append(
                WheelIssue(
                    "record-digest-algorithm-unknown",
                    f"RECORD entry for {normalized!r} uses unknown digest"
                    f" algorithm {algorithm!r}",
                    normalized,
                )
            )
            continue
        try:
            expected_size = int(size_text)
        except ValueError:
            issues.append(
                WheelIssue(
                    "record-size-invalid",
                    f"RECORD entry for {normalized!r} has no usable size",
                    normalized,
                )
            )
            continue

        # Verify every ZIP member stored under this path, so that duplicate
        # members cannot hide behind zipfile's read-by-name behaviour.
        for info in targets:
            try:
                actual_digest, actual_size = _hash_member(
                    archive, info, algorithm, budget
                )
            except _READ_ERRORS as exc:
                issues.append(
                    WheelIssue(
                        "member-read-error",
                        f"cannot read {normalized!r}: {exc}",
                        normalized,
                    )
                )
                continue
            if actual_digest != expected_digest.rstrip("="):
                issues.append(
                    WheelIssue(
                        "record-digest-mismatch",
                        f"digest mismatch for {normalized!r}: RECORD has"
                        f" {algorithm}={expected_digest}, member hashes to"
                        f" {actual_digest}",
                        normalized,
                    )
                )
            if actual_size != expected_size:
                issues.append(
                    WheelIssue(
                        "record-size-mismatch",
                        f"size mismatch for {normalized!r}: RECORD has"
                        f" {expected_size}, member is {actual_size} bytes",
                        normalized,
                    )
                )

    issues.extend(
        WheelIssue(
            "unrecorded-member",
            f"archive member {normalized!r} is not listed in RECORD",
            normalized,
        )
        for normalized in file_members
        if normalized not in special and normalized not in recorded
    )
