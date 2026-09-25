from __future__ import annotations

import textwrap

from packaging.metadata import (
    ChangeKind,
    ComparisonStatus,
    compare_metadata,
)


def _dedent(data: str) -> str:
    return textwrap.dedent(data).lstrip("\n")


class TestCompareMetadata:
    def test_identical_metadata_is_compatible(self) -> None:
        data = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Summary: a package
            """
        )
        result = compare_metadata(data, data)
        assert result.status is ComparisonStatus.COMPATIBLE
        assert result.changes == ()
        assert result.violations == ()

    def test_header_folding_changes_are_not_differences(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Summary: a fairly long summary that the sdist generator decided to
             fold onto a second line for readability
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Summary: a fairly long summary that the sdist generator decided
             to fold onto a second line for readability
            """
        )
        result = compare_metadata(sdist, wheel)
        assert result.status is ComparisonStatus.COMPATIBLE
        assert result.changes == ()

    def test_requires_dist_reordering_is_not_a_difference(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Requires-Dist: requests>=2.0
            Requires-Dist: click; python_version >= "3.8"
            Provides-Extra: Test
            Provides-Extra: docs
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Requires-Dist: click; python_version>="3.8"
            Requires-Dist: requests (>=2.0)
            Provides-Extra: docs
            Provides-Extra: test
            """
        )
        result = compare_metadata(sdist, wheel)
        assert result.status is ComparisonStatus.COMPATIBLE
        assert result.changes == ()

    def test_requires_dist_constraint_change_is_a_violation(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Requires-Dist: requests>=2.0
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Requires-Dist: requests>=3.0
            """
        )
        result = compare_metadata(sdist, wheel)
        assert result.status is ComparisonStatus.VIOLATED
        assert len(result.violations) == 1
        change = result.violations[0]
        assert change.field == "requires-dist"
        assert change.kind is ChangeKind.CHANGED
        assert change.sdist_value == ["requests>=2.0"]
        assert change.wheel_value == ["requests>=3.0"]
        assert not change.allowed
        assert "not declared Dynamic" in change.reason

    def test_dynamic_description_may_change(self) -> None:
        sdist = (
            _dedent(
                """
                Metadata-Version: 2.2
                Name: example
                Version: 1.0
                Dynamic: Description

                sdist-time description
                """
            )
            + "\n"
        )
        wheel = (
            _dedent(
                """
                Metadata-Version: 2.2
                Name: example
                Version: 1.0

                completely rewritten wheel description
                """
            )
            + "\n"
        )
        result = compare_metadata(sdist, wheel)
        assert result.status is ComparisonStatus.COMPATIBLE
        assert result.dynamic == ("description",)
        assert len(result.changes) == 1
        change = result.changes[0]
        assert change.field == "description"
        assert change.allowed
        assert "declared Dynamic" in change.reason
        assert result.violations == ()

    def test_dynamic_field_still_reported_as_change(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Summary: original
            Dynamic: Summary
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Summary: rewritten
            """
        )
        result = compare_metadata(sdist, wheel)
        assert result.status is ComparisonStatus.COMPATIBLE
        assert [c.kind for c in result.changes] == [ChangeKind.CHANGED]
        assert result.changes[0].allowed

    def test_name_and_version_compare_semantically(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: Foo_Bar
            Version: 1.0
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.2
            Name: foo-bar
            Version: 1.0
            """
        )
        assert compare_metadata(sdist, wheel).status is ComparisonStatus.COMPATIBLE

    def test_version_change_is_a_violation(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0.1
            """
        )
        result = compare_metadata(sdist, wheel)
        assert result.status is ComparisonStatus.VIOLATED
        assert result.violations[0].field == "version"

    def test_added_and_removed_fields_are_distinguished(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Home-Page: https://example.com
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Download-URL: https://example.com/files
            """
        )
        result = compare_metadata(sdist, wheel)
        assert result.status is ComparisonStatus.VIOLATED
        by_kind = {change.kind: change for change in result.violations}
        assert by_kind[ChangeKind.REMOVED].field == "home-page"
        assert by_kind[ChangeKind.REMOVED].sdist_value == "https://example.com"
        assert by_kind[ChangeKind.REMOVED].wheel_value is None
        assert by_kind[ChangeKind.ADDED].field == "download-url"
        assert by_kind[ChangeKind.ADDED].sdist_value is None
        assert by_kind[ChangeKind.ADDED].wheel_value == "https://example.com/files"

    def test_illegal_dynamic_use_is_distinguished(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Dynamic: Version
            Dynamic: Not-A-Field
            """
        )
        result = compare_metadata(sdist, sdist)
        assert result.status is ComparisonStatus.VIOLATED
        assert result.invalid_dynamic == ("version", "not-a-field")
        assert "illegal Dynamic usage" in result.reason

    def test_old_metadata_version_is_unknown_not_compatible(self) -> None:
        data = _dedent(
            """
            Metadata-Version: 1.2
            Name: example
            Version: 1.0
            """
        )
        result = compare_metadata(data, data)
        assert result.status is ComparisonStatus.UNKNOWN
        assert result.changes == ()
        assert "predates Dynamic" in result.reason

    def test_old_metadata_version_with_differences_is_still_unknown(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.1
            Name: example
            Version: 1.0
            Summary: one
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.1
            Name: example
            Version: 1.0
            Summary: two
            """
        )
        result = compare_metadata(sdist, wheel)
        assert result.status is ComparisonStatus.UNKNOWN
        # Differences are still reported for information.
        assert len(result.changes) == 1
        assert result.changes[0].field == "summary"

    def test_missing_metadata_version_is_unknown(self) -> None:
        data = _dedent(
            """
            Name: example
            Version: 1.0
            """
        )
        result = compare_metadata(data, data)
        assert result.status is ComparisonStatus.UNKNOWN
        assert result.metadata_version is None

    def test_keywords_and_project_urls_compare_by_value(self) -> None:
        sdist = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Keywords: alpha, beta
            Project-URL: Homepage, https://example.com
            """
        )
        wheel = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            Keywords: alpha,beta
            Project-URL: Homepage, https://example.com
            """
        )
        assert compare_metadata(sdist, wheel).status is ComparisonStatus.COMPATIBLE

    def test_bytes_input(self) -> None:
        data = _dedent(
            """
            Metadata-Version: 2.2
            Name: example
            Version: 1.0
            """
        ).encode()
        assert compare_metadata(data, data).status is ComparisonStatus.COMPATIBLE
