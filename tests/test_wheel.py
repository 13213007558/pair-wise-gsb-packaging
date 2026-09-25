# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
import warnings
import zipfile
from typing import BinaryIO, cast

import pytest

from packaging.wheel import WheelIssue, validate_wheel

METADATA = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
WHEEL = (
    b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
)


def _record_line(path: str, data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{path},sha256={encoded},{len(data)}"


class NamedBytesIO(io.BytesIO):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name


def make_wheel(
    *,
    filename: str = "demo-1.0-py3-none-any.whl",
    dist_info: str = "demo-1.0.dist-info",
    name: str = "demo",
    version: str = "1.0",
    tags: tuple[str, ...] = ("py3-none-any",),
    files: dict[str, bytes] | None = None,
    extra_members: list[tuple[str, bytes]] | tuple[tuple[str, bytes], ...] = (),
    record_rows: list[str] | None = None,
    omit_record: bool = False,
) -> NamedBytesIO:
    if files is None:
        files = {"demo/__init__.py": b"x = 1\n"}
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    wheel = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        + "".join(f"Tag: {tag}\n" for tag in tags)
    ).encode()
    members = dict(files)
    members[f"{dist_info}/METADATA"] = metadata
    members[f"{dist_info}/WHEEL"] = wheel
    if record_rows is None:
        record_rows = [_record_line(member, data) for member, data in members.items()]
        record_rows += [_record_line(member, data) for member, data in extra_members]
        record_rows.append(f"{dist_info}/RECORD,,")
    buffer = NamedBytesIO(filename)
    with warnings.catch_warnings():
        # zipfile warns about duplicate members; creating them is the point.
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w") as zf:
            for member, data in members.items():
                zf.writestr(member, data)
            for member, data in extra_members:
                zf.writestr(member, data)
            if not omit_record:
                zf.writestr(f"{dist_info}/RECORD", "\n".join(record_rows) + "\n")
    buffer.seek(0)
    return buffer


def default_record_rows(extra: list[str] | None = None) -> list[str]:
    rows = [
        _record_line("demo/__init__.py", b"x = 1\n"),
        _record_line("demo-1.0.dist-info/METADATA", METADATA),
        _record_line("demo-1.0.dist-info/WHEEL", WHEEL),
    ]
    if extra:
        rows.extend(extra)
    rows.append("demo-1.0.dist-info/RECORD,,")
    return rows


def kinds(issues: list[WheelIssue]) -> list[str]:
    return [issue.kind for issue in issues]


class TestValidateWheel:
    def test_valid_wheel_from_file_object(self) -> None:
        assert validate_wheel(make_wheel()) == []

    def test_valid_wheel_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "demo-1.0-py3-none-any.whl")
            with open(path, "wb") as f:
                f.write(make_wheel().getvalue())
            assert validate_wheel(path) == []
            with open(path, "rb") as f:
                assert validate_wheel(f) == []

    def test_valid_wheel_from_nameless_file_object(self) -> None:
        payload = io.BytesIO(make_wheel().getvalue())
        # Without a name, filename checks are skipped but content checks run.
        assert validate_wheel(payload) == []

    def test_non_seekable_file_object_rejected(self) -> None:
        class Stream(io.RawIOBase):
            def seekable(self) -> bool:
                return False

        with pytest.raises(ValueError, match="seekable"):
            validate_wheel(cast("BinaryIO", Stream()))

    def test_invalid_filename(self) -> None:
        issues = validate_wheel(make_wheel(filename="not-a-wheel.whl"))
        assert "invalid-filename" in kinds(issues)

    def test_not_a_zip(self) -> None:
        payload = NamedBytesIO("demo-1.0-py3-none-any.whl")
        payload.write(b"this is not a zip archive")
        payload.seek(0)
        issues = validate_wheel(payload)
        assert kinds(issues) == ["invalid-archive"]

    def test_tampered_member(self) -> None:
        files = {"demo/__init__.py": b"x = 1\n", "demo/data.txt": b"tampered\n"}
        rows = default_record_rows(extra=[_record_line("demo/data.txt", b"original\n")])
        issues = validate_wheel(make_wheel(files=files, record_rows=rows))
        assert kinds(issues) == ["record-digest-mismatch"]
        assert issues[0].member == "demo/data.txt"

    def test_record_size_mismatch(self) -> None:
        data = b"x = 1\n"
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
        rows = default_record_rows()
        rows[0] = (
            f"demo/__init__.py,sha256={digest.rstrip(b'=').decode()},{len(data) + 5}"
        )
        issues = validate_wheel(make_wheel(record_rows=rows))
        assert kinds(issues) == ["record-size-mismatch"]
        assert issues[0].member == "demo/__init__.py"

    def test_duplicate_member(self) -> None:
        wheel = make_wheel(
            extra_members=[("demo/__init__.py", b"x = 1\n")],
            record_rows=default_record_rows(),
        )
        issues = validate_wheel(wheel)
        assert kinds(issues) == ["duplicate-member"]
        assert issues[0].member == "demo/__init__.py"

    def test_duplicate_member_with_tampered_copy(self) -> None:
        # The tampered second copy must not hide behind the first one.
        wheel = make_wheel(
            extra_members=[("demo/__init__.py", b"x = 2\n")],
            record_rows=default_record_rows(),
        )
        issues = validate_wheel(wheel)
        assert kinds(issues) == ["duplicate-member", "record-digest-mismatch"]
        assert all(issue.member == "demo/__init__.py" for issue in issues)

    def test_path_collision_after_normalization(self) -> None:
        wheel = make_wheel(
            files={"demo/__init__.py": b"x = 1\n", "demo/other.py": b"y = 1\n"},
            extra_members=[("demo/./other.py", b"y = 1\n")],
        )
        issues = validate_wheel(wheel)
        assert "path-collision" in kinds(issues)
        collision = issues[kinds(issues).index("path-collision")]
        assert collision.member == "demo/./other.py"

    @pytest.mark.parametrize(
        "member",
        ["../evil.py", "demo/../../evil.py", "/abs/evil.py", "C:\\evil.py"],
    )
    def test_directory_traversal(self, member: str) -> None:
        issues = validate_wheel(make_wheel(extra_members=[(member, b"evil\n")]))
        assert "directory-traversal" in kinds(issues)
        traversal = issues[kinds(issues).index("directory-traversal")]
        assert traversal.member == member

    def test_record_entry_with_traversal_path(self) -> None:
        rows = default_record_rows(extra=["../evil.py,,"])
        issues = validate_wheel(make_wheel(record_rows=rows))
        assert "directory-traversal" in kinds(issues)

    def test_metadata_name_mismatch(self) -> None:
        issues = validate_wheel(make_wheel(name="other-demo"))
        assert kinds(issues) == ["metadata-name-mismatch"]
        assert issues[0].member == "demo-1.0.dist-info/METADATA"

    def test_metadata_version_mismatch(self) -> None:
        issues = validate_wheel(make_wheel(version="2.0"))
        assert kinds(issues) == ["metadata-version-mismatch"]
        assert issues[0].member == "demo-1.0.dist-info/METADATA"

    def test_metadata_version_invalid(self) -> None:
        issues = validate_wheel(make_wheel(version="not.a.version!!"))
        assert "metadata-version-invalid" in kinds(issues)

    def test_metadata_name_case_and_separator_normalized(self) -> None:
        wheel = make_wheel(filename="Demo-1.0-py3-none-any.whl", name="demo")
        assert validate_wheel(wheel) == []

    def test_wheel_tag_mismatch(self) -> None:
        wheel = make_wheel(tags=("cp312-abi3-manylinux_2_17_x86_64",))
        issues = validate_wheel(wheel)
        assert kinds(issues) == ["wheel-tags-mismatch"]
        assert issues[0].member == "demo-1.0.dist-info/WHEEL"

    def test_wheel_missing_tag(self) -> None:
        issues = validate_wheel(make_wheel(tags=()))
        assert "wheel-tags-missing" in kinds(issues)

    def test_missing_record(self) -> None:
        issues = validate_wheel(make_wheel(omit_record=True))
        assert kinds(issues) == ["missing-record"]

    def test_unrecorded_member(self) -> None:
        wheel = make_wheel(
            extra_members=[("demo/extra.py", b"z = 1\n")],
            record_rows=default_record_rows(),
        )
        issues = validate_wheel(wheel)
        assert kinds(issues) == ["unrecorded-member"]
        assert issues[0].member == "demo/extra.py"

    def test_record_lists_missing_member(self) -> None:
        rows = default_record_rows(extra=[_record_line("demo/ghost.py", b"boo\n")])
        issues = validate_wheel(make_wheel(record_rows=rows))
        assert kinds(issues) == ["record-missing-member"]
        assert issues[0].member == "demo/ghost.py"

    def test_record_unknown_digest_algorithm(self) -> None:
        rows = default_record_rows()
        rows[0] = "demo/__init__.py,rot13=abc,6"
        issues = validate_wheel(make_wheel(record_rows=rows))
        assert kinds(issues) == ["record-digest-algorithm-unknown"]
        assert issues[0].member == "demo/__init__.py"

    def test_record_entry_without_digest(self) -> None:
        rows = default_record_rows()
        rows[0] = "demo/__init__.py,,6"
        issues = validate_wheel(make_wheel(record_rows=rows))
        assert kinds(issues) == ["record-digest-missing"]

    def test_record_entry_without_size(self) -> None:
        data = b"x = 1\n"
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
        rows = default_record_rows()
        rows[0] = f"demo/__init__.py,sha256={digest.rstrip(b'=').decode()},"
        issues = validate_wheel(make_wheel(record_rows=rows))
        assert kinds(issues) == ["record-size-invalid"]

    def test_record_itself_must_have_empty_fields(self) -> None:
        rows = default_record_rows()
        rows[-1] = _record_line("demo-1.0.dist-info/RECORD", b"anything\n")
        issues = validate_wheel(make_wheel(record_rows=rows))
        assert kinds(issues) == ["record-special-entry"]
        assert issues[0].member == "demo-1.0.dist-info/RECORD"

    def test_record_signature_files_allowed_empty(self) -> None:
        rows = default_record_rows(
            extra=[
                "demo-1.0.dist-info/RECORD.jws,,",
                "demo-1.0.dist-info/RECORD.p7s,,",
            ]
        )
        wheel = make_wheel(
            extra_members=[
                ("demo-1.0.dist-info/RECORD.jws", b"sig-jws"),
                ("demo-1.0.dist-info/RECORD.p7s", b"sig-p7s"),
            ],
            record_rows=rows,
        )
        assert validate_wheel(wheel) == []

    def test_record_signature_file_with_digest_rejected(self) -> None:
        rows = default_record_rows(
            extra=[_record_line("demo-1.0.dist-info/RECORD.jws", b"sig-jws")]
        )
        wheel = make_wheel(
            extra_members=[("demo-1.0.dist-info/RECORD.jws", b"sig-jws")],
            record_rows=rows,
        )
        issues = validate_wheel(wheel)
        assert kinds(issues) == ["record-special-entry"]

    def test_multiple_dist_info(self) -> None:
        wheel = make_wheel(
            extra_members=[("other-2.0.dist-info/METADATA", b"Name: other\n")],
        )
        issues = validate_wheel(wheel)
        assert "multiple-dist-info" in kinds(issues)

    def test_missing_dist_info(self) -> None:
        buffer = NamedBytesIO("demo-1.0-py3-none-any.whl")
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("demo/__init__.py", b"x = 1\n")
        buffer.seek(0)
        issues = validate_wheel(buffer)
        assert kinds(issues) == ["missing-dist-info"]

    def test_dist_info_name_mismatch(self) -> None:
        issues = validate_wheel(make_wheel(dist_info="other-1.0.dist-info"))
        assert "dist-info-name-mismatch" in kinds(issues)

    def test_dist_info_version_mismatch(self) -> None:
        issues = validate_wheel(make_wheel(dist_info="demo-2.0.dist-info"))
        assert "dist-info-version-mismatch" in kinds(issues)

    def test_max_members_limit(self) -> None:
        issues = validate_wheel(make_wheel(), max_members=2)
        assert kinds(issues) == ["limit-exceeded"]

    def test_max_unpacked_bytes_limit_declared(self) -> None:
        issues = validate_wheel(make_wheel(), max_unpacked_bytes=10)
        assert kinds(issues) == ["limit-exceeded"]

    def test_corrupt_member_reported_not_raised(self) -> None:
        # Flip a payload byte without updating the CRC; the corrupt member
        # must be reported, not crash the validation.
        wheel = make_wheel(files={"demo/big.bin": b"x" * 4096})
        data = bytearray(wheel.getvalue())
        payload_offset = data.find(b"x" * 64)
        assert payload_offset != -1
        data[payload_offset] = ord("y")
        corrupt = NamedBytesIO("demo-1.0-py3-none-any.whl")
        corrupt.write(bytes(data))
        corrupt.seek(0)
        issues = validate_wheel(corrupt)
        assert "member-read-error" in kinds(issues)
        read_error = issues[kinds(issues).index("member-read-error")]
        assert read_error.member == "demo/big.bin"
