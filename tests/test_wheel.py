from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile

import pytest

from packaging.wheel import WheelIssue, validate_wheel

DIST_INFO = "demo-1.0.dist-info"
RECORD = f"{DIST_INFO}/RECORD"
WHEEL_FILENAME = "demo-1.0-py3-none-any.whl"


def _record_line(path: str, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"{path},sha256={digest},{len(data)}"


def refresh_record(members: dict[str, bytes]) -> dict[str, bytes]:
    lines = [
        _record_line(path, data) for path, data in members.items() if path != RECORD
    ]
    lines.append(f"{RECORD},,")
    members[RECORD] = ("\n".join(lines) + "\n").encode()
    return members


def make_members() -> dict[str, bytes]:
    members = {
        "demo/__init__.py": b"",
        "demo/data.txt": b"payload",
        f"{DIST_INFO}/METADATA": (b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"),
        f"{DIST_INFO}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    return refresh_record(members)


def make_wheel(
    members: dict[str, bytes] | None = None,
    *,
    duplicates: list[tuple[str, bytes]] | None = None,
    filename: str | None = WHEEL_FILENAME,
) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in (members if members is not None else make_members()).items():
            zf.writestr(name, data)
        for name, data in duplicates or []:
            zf.writestr(name, data)
    buffer.seek(0)
    if filename is not None:
        buffer.name = filename
    return buffer


def codes(issues: list[WheelIssue]) -> list[str]:
    return [issue.code for issue in issues]


def members_for(issues: list[WheelIssue], code: str) -> list[str | None]:
    return [issue.member for issue in issues if issue.code == code]


class TestValidWheel:
    def test_no_issues(self) -> None:
        assert validate_wheel(make_wheel()) == []

    def test_no_issues_from_path_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, WHEEL_FILENAME)
            with open(path, "wb") as f:
                f.write(make_wheel().getvalue())
            assert validate_wheel(path) == []
            assert validate_wheel(os.fspath(path)) == []

    def test_signature_files_may_be_unlisted(self) -> None:
        members = make_members()
        members[f"{RECORD}.jws"] = b"signature"
        members[f"{RECORD}.p7s"] = b"signature"
        assert validate_wheel(make_wheel(members)) == []


class TestFilename:
    def test_invalid_filename(self) -> None:
        issues = validate_wheel(make_wheel(filename="demo-1.0-py3-none-any.zip"))
        assert "invalid-filename" in codes(issues)

    def test_missing_filename_skips_filename_checks(self) -> None:
        assert validate_wheel(make_wheel(filename=None)) == []

    def test_bad_zip(self) -> None:
        buffer = io.BytesIO(b"this is not a zip archive")
        buffer.name = WHEEL_FILENAME
        issues = validate_wheel(buffer)
        assert codes(issues) == ["bad-zip"]


class TestMembers:
    def test_duplicate_member(self) -> None:
        with pytest.warns(UserWarning, match="Duplicate name"):
            wheel = make_wheel(duplicates=[("demo/data.txt", b"other")])
        issues = validate_wheel(wheel)
        assert members_for(issues, "duplicate-member") == ["demo/data.txt"]

    def test_path_collision_after_normalization(self) -> None:
        members = make_members()
        members["demo/./sub.py"] = b""
        members["demo/sub.py"] = b""
        issues = validate_wheel(make_wheel(refresh_record(members)))
        assert members_for(issues, "path-collision") == ["demo/sub.py"]

    @pytest.mark.parametrize(
        "name", ["../evil.py", "demo/../../evil.py", "/abs/path.py", "C:/evil.py"]
    )
    def test_path_traversal(self, name: str) -> None:
        members = make_members()
        members[name] = b""
        issues = validate_wheel(make_wheel(refresh_record(members)))
        assert members_for(issues, "path-traversal") == [name]

    def test_too_many_members(self) -> None:
        issues = validate_wheel(make_wheel(), max_members=2)
        assert "too-many-members" in codes(issues)

    def test_multiple_dist_info_dirs(self) -> None:
        members = make_members()
        members["other-2.0.dist-info/METADATA"] = b"Name: other\nVersion: 2.0\n"
        issues = validate_wheel(make_wheel(refresh_record(members)))
        assert "dist-info-count" in codes(issues)

    def test_no_dist_info_dir(self) -> None:
        members = {"demo/__init__.py": b""}
        issues = validate_wheel(make_wheel(members))
        assert "dist-info-count" in codes(issues)


class TestMetadata:
    def test_name_mismatch(self) -> None:
        members = make_members()
        members[f"{DIST_INFO}/METADATA"] = (
            b"Metadata-Version: 2.1\nName: other\nVersion: 1.0\n"
        )
        issues = validate_wheel(make_wheel(refresh_record(members)))
        assert members_for(issues, "metadata-name-mismatch") == [
            f"{DIST_INFO}/METADATA"
        ]

    def test_version_mismatch(self) -> None:
        members = make_members()
        members[f"{DIST_INFO}/METADATA"] = (
            b"Metadata-Version: 2.1\nName: demo\nVersion: 2.0\n"
        )
        issues = validate_wheel(make_wheel(refresh_record(members)))
        assert members_for(issues, "metadata-version-mismatch") == [
            f"{DIST_INFO}/METADATA"
        ]

    def test_missing_metadata(self) -> None:
        members = make_members()
        del members[f"{DIST_INFO}/METADATA"]
        issues = validate_wheel(make_wheel(refresh_record(members)))
        assert members_for(issues, "missing-file") == [f"{DIST_INFO}/METADATA"]

    def test_wheel_tag_mismatch(self) -> None:
        members = make_members()
        members[f"{DIST_INFO}/WHEEL"] = (
            b"Wheel-Version: 1.0\n"
            b"Generator: test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: cp312-cp312-manylinux_2_17_x86_64\n"
        )
        issues = validate_wheel(make_wheel(refresh_record(members)))
        assert members_for(issues, "wheel-tag-mismatch") == [f"{DIST_INFO}/WHEEL"]

    def test_wheel_missing_tag(self) -> None:
        members = make_members()
        members[f"{DIST_INFO}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: test\n"
        issues = validate_wheel(make_wheel(refresh_record(members)))
        assert members_for(issues, "wheel-missing-tag") == [f"{DIST_INFO}/WHEEL"]


class TestRecord:
    def test_corrupted_data_file(self) -> None:
        members = make_members()
        members["demo/data.txt"] = b"tampered"
        # RECORD intentionally left stale.
        issues = validate_wheel(make_wheel(members))
        assert members_for(issues, "digest-mismatch") == ["demo/data.txt"]

    def test_size_mismatch(self) -> None:
        members = make_members()
        record = members[RECORD].decode()
        members[RECORD] = record.replace(
            _record_line("demo/data.txt", b"payload"),
            "demo/data.txt,sha256=" + hashlib.sha256(b"payload").hexdigest() + ",999",
        ).encode()
        issues = validate_wheel(make_wheel(members))
        assert members_for(issues, "size-mismatch") == ["demo/data.txt"]

    def test_missing_record_entry(self) -> None:
        members = make_members()
        record = members[RECORD].decode()
        members[RECORD] = record.replace(
            _record_line("demo/data.txt", b"payload") + "\n", ""
        ).encode()
        issues = validate_wheel(make_wheel(members))
        assert members_for(issues, "record-missing-entry") == ["demo/data.txt"]

    def test_unexpected_record_entry(self) -> None:
        members = make_members()
        record = members[RECORD].decode()
        ghost = _record_line("demo/ghost.py", b"boo")
        members[RECORD] = record.replace(f"{RECORD},,", f"{ghost}\n{RECORD},,").encode()
        issues = validate_wheel(make_wheel(members))
        assert members_for(issues, "record-unexpected-entry") == ["demo/ghost.py"]

    def test_record_self_entry_must_be_empty(self) -> None:
        members = make_members()
        record = members[RECORD].decode()
        members[RECORD] = record.replace(
            f"{RECORD},,", _record_line(RECORD, record.encode())
        ).encode()
        issues = validate_wheel(make_wheel(members))
        assert members_for(issues, "record-exempt-entry") == [RECORD]

    def test_weak_digest_algorithm(self) -> None:
        members = make_members()
        record = members[RECORD].decode()
        members[RECORD] = record.replace(
            _record_line("demo/data.txt", b"payload"),
            "demo/data.txt,md5="
            + hashlib.md5(b"payload", usedforsecurity=False).hexdigest()
            + ",7",
        ).encode()
        issues = validate_wheel(make_wheel(members))
        assert members_for(issues, "record-weak-hash") == ["demo/data.txt"]
        assert "digest-mismatch" not in codes(issues)

    def test_archive_too_large(self) -> None:
        issues = validate_wheel(make_wheel(), max_unpacked_bytes=1)
        assert "archive-too-large" in codes(issues)

    def test_missing_record_file(self) -> None:
        members = make_members()
        del members[RECORD]
        issues = validate_wheel(make_wheel(members))
        assert members_for(issues, "missing-file") == [RECORD]


class TestSourceTypes:
    def test_non_seekable_file_rejected(self) -> None:
        class NonSeekable(io.BytesIO):
            def seekable(self) -> bool:
                return False

        with pytest.raises(ValueError, match="seekable"):
            validate_wheel(NonSeekable(b"data"))


def test_issue_str_includes_member() -> None:
    issue = WheelIssue("digest-mismatch", "bad digest", member="demo/data.txt")
    assert str(issue) == "[digest-mismatch] demo/data.txt: bad digest"
    issue = WheelIssue("bad-zip", "not a zip")
    assert str(issue) == "[bad-zip] not a zip"
