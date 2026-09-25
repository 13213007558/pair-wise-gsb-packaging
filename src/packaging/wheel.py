# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import os
import zipfile
from email.parser import Parser
from typing import IO

from .tags import InvalidTag, Tag, parse_tag
from .utils import (
    BuildTag,
    InvalidName,
    InvalidWheelFilename,
    NormalizedName,
    canonicalize_name,
    parse_wheel_filename,
)
from .version import InvalidVersion, Version

__all__ = ["WheelIssue", "validate_wheel"]


_CHUNK_SIZE = 64 * 1024
_WEAK_DIGEST_ALGORITHMS = {"md5", "sha1"}
_SIGNATURE_SUFFIXES = (".jws", ".p7s")

_ParsedFilename = tuple[NormalizedName, Version, BuildTag, frozenset[Tag]]


@dataclasses.dataclass(frozen=True)
class WheelIssue:
    """
    A single problem found while validating a wheel.

    'code' is a stable machine-readable identifier, 'message' a
    human-readable description, and 'member' the path of the archive
    member the issue relates to ('None' for archive-level issues).
    """

    code: str
    message: str
    member: str | None = None

    def __str__(self) -> str:
        if self.member is None:
            return f"[{self.code}] {self.message}"
        return f"[{self.code}] {self.member}: {self.message}"


def validate_wheel(
    source: str | os.PathLike[str] | IO[bytes],
    *,
    max_members: int = 10_000,
    max_unpacked_bytes: int = 1 << 30,
) -> list[WheelIssue]:
    """
    Validate a wheel archive without installing it or importing its code.

    'source' may be a path to a .whl file or a seekable binary file
    object. The archive is only read, never extracted to disk.

    Returns a list of 'WheelIssue'; an empty list means no problems were
    found. Individual issues carry the archive member they relate to, so
    callers can present an independent issue list.

    :param source: Path or seekable binary file object of the wheel.
    :param max_members: Maximum number of ZIP members allowed.
    :param max_unpacked_bytes: Maximum total decompressed bytes allowed
        while verifying RECORD digests.
    :raises ValueError: If 'source' is a file object that is not seekable.
    """
    issues: list[WheelIssue] = []

    if isinstance(source, (str, os.PathLike)):
        filename: str | None = os.path.basename(os.fspath(source))
    else:
        if not source.seekable():
            raise ValueError("validate_wheel() requires a seekable file object")
        name = getattr(source, "name", None)
        filename = os.path.basename(name) if isinstance(name, str) else None

    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as e:
        return [WheelIssue("bad-zip", f"not a valid ZIP archive: {e}")]

    with archive:
        infos = archive.infolist()
        members: dict[str, list[zipfile.ZipInfo]] = {}
        for info in infos:
            members.setdefault(info.filename, []).append(info)

        _check_members(infos, issues, max_members=max_members)

        expected = None
        if filename is not None:
            try:
                expected = parse_wheel_filename(filename)
            except InvalidWheelFilename as e:
                issues.append(WheelIssue("invalid-filename", str(e)))

        dist_infos = sorted(
            {
                info.filename.split("/", 1)[0]
                for info in infos
                if "/" in info.filename
                and info.filename.split("/", 1)[0].endswith(".dist-info")
            }
        )
        if len(dist_infos) != 1:
            issues.append(
                WheelIssue(
                    "dist-info-count",
                    f"expected exactly one .dist-info directory, "
                    f"found {len(dist_infos)}",
                )
            )
        if not dist_infos:
            return issues
        dist_info = dist_infos[0]

        _check_metadata(archive, members, dist_info, expected, issues)
        _check_wheel_file(archive, members, dist_info, expected, issues)
        _check_record(
            archive,
            members,
            dist_info,
            issues,
            max_unpacked_bytes=max_unpacked_bytes,
        )

    return issues


def _read_member(
    archive: zipfile.ZipFile,
    members: dict[str, list[zipfile.ZipInfo]],
    name: str,
) -> bytes | None:
    infos = members.get(name)
    if not infos:
        return None
    try:
        with archive.open(infos[0]) as fp:
            return fp.read()
    except (zipfile.BadZipFile, RuntimeError):
        return None


def _check_members(
    infos: list[zipfile.ZipInfo],
    issues: list[WheelIssue],
    *,
    max_members: int,
) -> None:
    if len(infos) > max_members:
        issues.append(
            WheelIssue(
                "too-many-members",
                f"archive has {len(infos)} members, limit is {max_members}",
            )
        )

    seen: set[str] = set()
    normalized: dict[str, list[str]] = {}
    for info in infos:
        name = info.filename
        if name in seen:
            # zipfile reads members by name would silently pick one of the
            # duplicates, so report every duplicate explicitly.
            issues.append(
                WheelIssue("duplicate-member", "duplicate ZIP member", member=name)
            )
            continue
        seen.add(name)

        components = name.split("/")
        if (
            name.startswith(("/", "\\"))
            or ".." in components
            or "\\" in name
            or (len(name) >= 2 and name[1] == ":")
        ):
            issues.append(
                WheelIssue(
                    "path-traversal",
                    "member path is absolute or escapes the archive root",
                    member=name,
                )
            )

        norm = "/".join(p for p in components if p not in ("", "."))
        if name.endswith("/") and norm:
            norm += "/"
        normalized.setdefault(norm or name, []).append(name)

    for norm, names in sorted(normalized.items()):
        if len(names) > 1:
            issues.append(
                WheelIssue(
                    "path-collision",
                    f"members {names} normalize to the same path",
                    member=norm,
                )
            )


def _check_metadata(
    archive: zipfile.ZipFile,
    members: dict[str, list[zipfile.ZipInfo]],
    dist_info: str,
    expected: _ParsedFilename | None,
    issues: list[WheelIssue],
) -> None:
    metadata_name = f"{dist_info}/METADATA"
    raw = _read_member(archive, members, metadata_name)
    if raw is None:
        issues.append(
            WheelIssue("missing-file", "METADATA is missing", member=metadata_name)
        )
        return

    headers = Parser().parsestr(raw.decode("utf-8", "replace"))

    name = headers.get("Name")
    if name is None:
        issues.append(
            WheelIssue(
                "metadata-missing-field", "METADATA has no Name", member=metadata_name
            )
        )
    else:
        try:
            canonical = canonicalize_name(name, validate=True)
        except InvalidName as e:
            issues.append(
                WheelIssue("metadata-invalid-name", str(e), member=metadata_name)
            )
        else:
            if expected is not None and canonical != expected[0]:
                issues.append(
                    WheelIssue(
                        "metadata-name-mismatch",
                        f"METADATA Name {name!r} does not match filename",
                        member=metadata_name,
                    )
                )

    version = headers.get("Version")
    if version is None:
        issues.append(
            WheelIssue(
                "metadata-missing-field",
                "METADATA has no Version",
                member=metadata_name,
            )
        )
    else:
        try:
            parsed_version = Version(version)
        except InvalidVersion as e:
            issues.append(
                WheelIssue("metadata-invalid-version", str(e), member=metadata_name)
            )
        else:
            if expected is not None and parsed_version != expected[1]:
                issues.append(
                    WheelIssue(
                        "metadata-version-mismatch",
                        f"METADATA Version {version!r} does not match filename",
                        member=metadata_name,
                    )
                )


def _parse_tag_value(value: str) -> tuple[frozenset[Tag], str | None]:
    try:
        return parse_tag(value), None
    except InvalidTag as e:
        return frozenset(), str(e)


def _check_wheel_file(
    archive: zipfile.ZipFile,
    members: dict[str, list[zipfile.ZipInfo]],
    dist_info: str,
    expected: _ParsedFilename | None,
    issues: list[WheelIssue],
) -> None:
    wheel_name = f"{dist_info}/WHEEL"
    raw = _read_member(archive, members, wheel_name)
    if raw is None:
        issues.append(WheelIssue("missing-file", "WHEEL is missing", member=wheel_name))
        return

    headers = Parser().parsestr(raw.decode("utf-8", "replace"))

    if headers.get("Wheel-Version") is None:
        issues.append(
            WheelIssue(
                "wheel-missing-version",
                "WHEEL has no Wheel-Version",
                member=wheel_name,
            )
        )

    tag_values = headers.get_all("Tag", [])
    if not tag_values:
        issues.append(
            WheelIssue("wheel-missing-tag", "WHEEL has no Tag", member=wheel_name)
        )
    wheel_tags: set[Tag] = set()
    for value in tag_values:
        parsed, error = _parse_tag_value(value)
        if error is not None:
            issues.append(WheelIssue("wheel-invalid-tag", error, member=wheel_name))
        wheel_tags |= parsed
    if expected is not None and wheel_tags and not (wheel_tags & expected[3]):
        issues.append(
            WheelIssue(
                "wheel-tag-mismatch",
                "no WHEEL Tag matches the filename tags",
                member=wheel_name,
            )
        )


def _check_record(
    archive: zipfile.ZipFile,
    members: dict[str, list[zipfile.ZipInfo]],
    dist_info: str,
    issues: list[WheelIssue],
    *,
    max_unpacked_bytes: int,
) -> None:
    record_name = f"{dist_info}/RECORD"
    raw = _read_member(archive, members, record_name)
    if raw is None:
        issues.append(
            WheelIssue("missing-file", "RECORD is missing", member=record_name)
        )
        return

    # RECORD itself and the optional signature files must carry empty
    # hash and size fields; the signature files may also be absent from
    # RECORD entirely.
    exempt = {record_name, *(record_name + suffix for suffix in _SIGNATURE_SUFFIXES)}
    optional = {record_name + suffix for suffix in _SIGNATURE_SUFFIXES}

    recorded: set[str] = set()
    verifiable: list[tuple[str, str, str, int]] = []
    for lineno, row in enumerate(
        csv.reader(io.StringIO(raw.decode("utf-8", "replace"))), start=1
    ):
        if not row:
            continue
        if len(row) != 3:
            issues.append(
                WheelIssue(
                    "record-row-malformed",
                    f"RECORD line {lineno} has {len(row)} fields, expected 3",
                    member=record_name,
                )
            )
            continue
        path, hash_field, size_field = row
        if not path:
            issues.append(
                WheelIssue(
                    "record-row-malformed",
                    f"RECORD line {lineno} has an empty path",
                    member=record_name,
                )
            )
            continue
        if path in recorded:
            issues.append(
                WheelIssue(
                    "record-duplicate-entry", "duplicate RECORD entry", member=path
                )
            )
            continue
        recorded.add(path)

        if path in exempt:
            if hash_field or size_field:
                issues.append(
                    WheelIssue(
                        "record-exempt-entry",
                        "RECORD and signature entries must have empty hash and size",
                        member=path,
                    )
                )
            continue

        if not hash_field:
            issues.append(
                WheelIssue(
                    "record-missing-hash", "RECORD entry has no hash", member=path
                )
            )
            continue
        algorithm, separator, hexdigest = hash_field.partition("=")
        if not separator or not hexdigest:
            issues.append(
                WheelIssue(
                    "record-bad-hash",
                    f"RECORD hash {hash_field!r} is not 'algorithm=digest'",
                    member=path,
                )
            )
            continue
        try:
            hashlib.new(algorithm)
        except (ValueError, TypeError):
            issues.append(
                WheelIssue(
                    "record-unknown-hash",
                    f"unknown digest algorithm {algorithm!r}",
                    member=path,
                )
            )
            continue
        if algorithm.lower() in _WEAK_DIGEST_ALGORITHMS:
            issues.append(
                WheelIssue(
                    "record-weak-hash",
                    f"weak digest algorithm {algorithm!r}",
                    member=path,
                )
            )

        if not size_field:
            issues.append(
                WheelIssue(
                    "record-missing-size", "RECORD entry has no size", member=path
                )
            )
            continue
        try:
            size = int(size_field)
        except ValueError:
            issues.append(
                WheelIssue(
                    "record-bad-size",
                    f"RECORD size {size_field!r} is not an integer",
                    member=path,
                )
            )
            continue
        if size < 0:
            issues.append(
                WheelIssue(
                    "record-bad-size",
                    f"RECORD size {size_field!r} is negative",
                    member=path,
                )
            )
            continue

        verifiable.append((path, algorithm, hexdigest, size))

    for name in sorted(members):
        if name.endswith("/") or name in optional:
            continue
        if name not in recorded:
            issues.append(
                WheelIssue(
                    "record-missing-entry", "file is not listed in RECORD", member=name
                )
            )

    issues.extend(
        WheelIssue(
            "record-unexpected-entry",
            "RECORD entry has no matching archive member",
            member=path,
        )
        for path in sorted(recorded)
        if path not in members
    )

    total_bytes = 0
    for path, algorithm, hexdigest, size in verifiable:
        infos = members.get(path)
        if not infos or len(infos) > 1 or infos[0].is_dir():
            # Missing members were reported above; duplicates are
            # ambiguous to verify and were reported as duplicate-member.
            continue
        hasher = hashlib.new(algorithm)
        count = 0
        try:
            with archive.open(infos[0]) as fp:
                while chunk := fp.read(_CHUNK_SIZE):
                    hasher.update(chunk)
                    count += len(chunk)
                    total_bytes += len(chunk)
                    if total_bytes > max_unpacked_bytes:
                        issues.append(
                            WheelIssue(
                                "archive-too-large",
                                f"decompressed data exceeds the limit of "
                                f"{max_unpacked_bytes} bytes",
                                member=path,
                            )
                        )
                        return
        except (zipfile.BadZipFile, RuntimeError) as e:
            issues.append(
                WheelIssue("member-unreadable", f"cannot read member: {e}", member=path)
            )
            continue
        if count != size:
            issues.append(
                WheelIssue(
                    "size-mismatch",
                    f"RECORD declares {size} bytes, actual size is {count}",
                    member=path,
                )
            )
        if hasher.hexdigest() != hexdigest.lower():
            issues.append(
                WheelIssue(
                    "digest-mismatch",
                    "member digest does not match RECORD",
                    member=path,
                )
            )
