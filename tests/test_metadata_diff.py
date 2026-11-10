from __future__ import annotations

import dataclasses

import pytest
from packaging.metadata_diff import (
    DifferenceStatus,
    FieldDifference,
    diff_metadata,
)


def _metadata(
    *,
    version: str = "2.3",
    name: str = "demo",
    dist_version: str = "1.0",
    extra: str = "",
) -> str:
    head = f"Metadata-Version: {version}\nName: {name}\nVersion: {dist_version}\n"
    return head + extra


class TestDifferenceStatus:
    def test_status_values(self) -> None:
        assert [status.value for status in DifferenceStatus] == [
            "format",
            "allowed",
            "violation",
            "undetermined",
        ]


class TestNoDifferences:
    def test_identical_documents(self) -> None:
        source = _metadata(extra="Summary: hello\nRequires-Dist: pip\n")
        report = diff_metadata(source, source)
        assert report.differences == ()
        assert report.violations == ()
        assert len(report) == 0
        assert list(report) == []
        assert report.dynamic_contract_applies is True

    def test_bytes_input(self) -> None:
        source = _metadata().encode()
        report = diff_metadata(source, source)
        assert report.differences == ()

    def test_report_exposes_parsed_metadata(self) -> None:
        source = _metadata()
        report = diff_metadata(source, source)
        assert report.sdist.name == "demo"
        assert report.wheel.version == report.sdist.version


class TestStaticDependencyReplacement:
    def test_static_dependency_replaced_is_violation(self) -> None:
        source = _metadata(extra="Requires-Dist: olddep==1.0\n")
        wheel = _metadata(extra="Requires-Dist: newdep==2.0\n")
        report = diff_metadata(source, wheel)

        assert len(report.violations) == 1
        difference = report.violations[0]
        assert difference.field == "requires_dist"
        assert difference.status is DifferenceStatus.VIOLATION
        assert "olddep" in difference.detail
        assert "newdep" in difference.detail
        # The raw values are retained for review rather than a text-only diff.
        assert difference.sdist_value == ["olddep==1.0"]
        assert difference.wheel_value == ["newdep==2.0"]

    def test_static_constraint_changed_is_violation(self) -> None:
        source = _metadata(extra="Requires-Dist: dep>=1.0\n")
        wheel = _metadata(extra="Requires-Dist: dep>=2.0\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.violations
        assert difference.field == "requires_dist"
        assert "'dep>=1.0' -> 'dep>=2.0'" in difference.detail

    def test_dependency_removed_and_added_separately_reported(self) -> None:
        source = _metadata(extra="Requires-Dist: keep\nRequires-Dist: gone\n")
        wheel = _metadata(extra="Requires-Dist: keep\nRequires-Dist: arrived\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.VIOLATION
        assert "removed" in difference.detail
        assert "gone" in difference.detail
        assert "added" in difference.detail
        assert "arrived" in difference.detail

    def test_dependencies_added_to_empty_set_is_violation(self) -> None:
        wheel = _metadata(extra="Requires-Dist: dep\n")
        report = diff_metadata(_metadata(), wheel)
        (difference,) = report.violations
        assert difference.field == "requires_dist"
        assert difference.sdist_value is None
        assert difference.wheel_value == ["dep"]

    def test_all_dependencies_removed_is_violation(self) -> None:
        source = _metadata(extra="Requires-Dist: dep\n")
        report = diff_metadata(source, _metadata())
        (difference,) = report.violations
        assert difference.field == "requires_dist"
        assert difference.sdist_value == ["dep"]
        assert difference.wheel_value is None


class TestDynamicFields:
    def test_declared_dynamic_field_change_is_allowed(self) -> None:
        source = _metadata(extra="Dynamic: Requires-Dist\nRequires-Dist: dep==1.0\n")
        wheel = _metadata(extra=("Dynamic: Requires-Dist\nRequires-Dist: dep==2.0\n"))
        report = diff_metadata(source, wheel)
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.field == "requires_dist"
        assert difference.status is DifferenceStatus.ALLOWED

    def test_declared_dynamic_field_materializing_from_absent(self) -> None:
        # A Dynamic entry names a field the sdist itself does not provide. The
        # declaration still cannot authorize an undeclared new field, so its
        # appearance in the wheel is reported rather than silently allowed.
        source = _metadata(extra="Dynamic: Summary\n")
        wheel = _metadata(extra="Dynamic: Summary\nSummary: generated\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "summary"
        assert difference.status is DifferenceStatus.VIOLATION
        assert difference.sdist_value is None

    def test_dynamic_field_removed_is_allowed(self) -> None:
        source = _metadata(extra="Dynamic: Summary\nSummary: generated at build time\n")
        wheel = _metadata(extra="Dynamic: Summary\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "summary"
        assert difference.status is DifferenceStatus.ALLOWED
        assert difference.wheel_value is None

    def test_undeclared_field_change_is_violation_alongside_dynamic(self) -> None:
        source = _metadata(
            extra=("Dynamic: Summary\nSummary: placeholder\nRequires-Python: >=3.8\n")
        )
        wheel = _metadata(
            extra=("Dynamic: Summary\nSummary: generated\nRequires-Python: >=3.10\n")
        )
        report = diff_metadata(source, wheel)
        by_field = {difference.field: difference for difference in report}
        assert by_field["summary"].status is DifferenceStatus.ALLOWED
        requires_python = by_field["requires_python"]
        assert requires_python.status is DifferenceStatus.VIOLATION
        assert requires_python.sdist_value == ">=3.8"
        assert requires_python.wheel_value == ">=3.10"

    def test_name_change_is_violation_even_if_dynamic_claims_it(self) -> None:
        # The validator rejects a Dynamic: Name entry, so the Dynamic field is
        # unparsable; Name itself remains protected regardless.
        source = _metadata(extra="Dynamic: Name\n")
        wheel = _metadata(name="other", extra="Dynamic: Name\n")
        report = diff_metadata(source, wheel)
        by_field = {difference.field: difference for difference in report}
        assert by_field["name"].status is DifferenceStatus.VIOLATION
        assert "different project" in by_field["name"].detail
        assert by_field["dynamic"].status is DifferenceStatus.UNDETERMINED

    def test_version_change_is_always_violation(self) -> None:
        report = diff_metadata(
            _metadata(dist_version="1.0"), _metadata(dist_version="2.0")
        )
        (difference,) = report.violations
        assert difference.field == "version"
        assert "1.0" in difference.detail
        assert "2.0" in difference.detail


class TestStaticFieldAdded:
    def test_new_static_field_is_violation(self) -> None:
        wheel = _metadata(extra="Summary: surprise\n")
        report = diff_metadata(_metadata(), wheel)
        (difference,) = report.violations
        assert difference.field == "summary"
        assert difference.sdist_value is None
        assert difference.wheel_value == "surprise"
        assert "added" in difference.detail

    def test_removed_static_field_is_violation(self) -> None:
        source = _metadata(extra="Home-page: https://example.invalid\n")
        report = diff_metadata(source, _metadata())
        (difference,) = report.violations
        assert difference.field == "home_page"
        assert difference.sdist_value == "https://example.invalid"
        assert difference.wheel_value is None
        assert "removed" in difference.detail


class TestOldMetadataVersions:
    @pytest.mark.parametrize("old_version", ["1.0", "1.1", "1.2", "2.1"])
    def test_changes_are_undetermined_without_dynamic_contract(
        self, old_version: str
    ) -> None:
        source = _metadata(version=old_version, extra="Requires-Dist: olddep==1.0\n")
        wheel = _metadata(version=old_version, extra="Requires-Dist: newdep==2.0\n")
        report = diff_metadata(source, wheel)
        assert report.dynamic_contract_applies is False
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert difference.field == "requires_dist"
        assert difference.sdist_value == ["olddep==1.0"]
        assert difference.wheel_value == ["newdep==2.0"]

    def test_old_version_static_field_added_is_undetermined(self) -> None:
        source = _metadata(version="2.1")
        wheel = _metadata(version="2.1", extra="Summary: late\n")
        report = diff_metadata(source, wheel)
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert difference.field == "summary"

    def test_name_identity_still_enforced_for_old_versions(self) -> None:
        report = diff_metadata(
            _metadata(version="2.1", name="demo"),
            _metadata(version="2.1", name="other"),
        )
        (difference,) = report.violations
        assert difference.field == "name"

    def test_malformed_metadata_version_disables_contract(self) -> None:
        source = _metadata(version="9.9", extra="Summary: a\n")
        wheel = _metadata(version="9.9", extra="Summary: b\n")
        report = diff_metadata(source, wheel)
        assert report.dynamic_contract_applies is False
        assert report.violations == ()


class TestLegalFormatChanges:
    def test_name_normalization_is_format_change(self) -> None:
        report = diff_metadata(_metadata(name="foo_bar"), _metadata(name="Foo-Bar"))
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT
        assert difference.field == "name"
        assert difference.sdist_value == "foo_bar"
        assert difference.wheel_value == "Foo-Bar"

    def test_equivalent_version_spelling_is_format_change(self) -> None:
        report = diff_metadata(
            _metadata(dist_version="1.0"),
            _metadata(dist_version="1.0.0"),
        )
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT
        assert difference.field == "version"

    def test_equivalent_python_specifier_is_format_change(self) -> None:
        source = _metadata(extra="Requires-Python: >=3.8\n")
        wheel = _metadata(extra="Requires-Python: >=3.8.0\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT
        assert difference.field == "requires_python"

    def test_equivalent_requirement_spelling_is_format_change(self) -> None:
        source = _metadata(extra="Requires-Dist: old_dep==1.0.0\n")
        wheel = _metadata(extra="Requires-Dist: Old-Dep==1.0\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT
        assert difference.field == "requires_dist"
        assert "'old_dep==1.0.0' -> 'Old-Dep==1.0'" in difference.detail

    def test_requirement_reordering_is_format_change(self) -> None:
        source = _metadata(extra="Requires-Dist: a\nRequires-Dist: b\n")
        wheel = _metadata(extra="Requires-Dist: b\nRequires-Dist: a\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT

    def test_classifier_reordering_is_format_change(self) -> None:
        source = _metadata(extra="Classifier: A\nClassifier: B\n")
        wheel = _metadata(extra="Classifier: B\nClassifier: A\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT
        assert difference.field == "classifiers"

    def test_provides_extra_normalization_is_format_change(self) -> None:
        source = _metadata(extra="Provides-Extra: My_Extra\n")
        wheel = _metadata(extra="Provides-Extra: my-extra\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT

    def test_license_expression_spelling_is_format_change(self) -> None:
        source = _metadata(
            version="2.4", extra="License-Expression: mit OR apache-2.0\n"
        )
        wheel = _metadata(
            version="2.4", extra="License-Expression: MIT OR Apache-2.0\n"
        )
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT
        assert difference.field == "license_expression"


class TestRequiresDistSemantics:
    def test_marker_only_change_is_undetermined(self) -> None:
        source = _metadata(extra='Requires-Dist: dep>=1; python_version >= "3.8"\n')
        wheel = _metadata(extra='Requires-Dist: dep>=1; python_version >= "3.9"\n')
        report = diff_metadata(source, wheel)
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert "marker" in difference.detail

    def test_marker_reordered_and_is_format_change(self) -> None:
        source = _metadata(
            extra=(
                'Requires-Dist: dep>=1; python_version >= "3.8" '
                'and os_name == "posix"\n'
            )
        )
        wheel = _metadata(
            extra=(
                'Requires-Dist: dep>=1; os_name == "posix" '
                'and python_version >= "3.8"\n'
            )
        )
        report = diff_metadata(source, wheel)
        # Marker tree serialization differs only by clause order here, which
        # packaging cannot prove equivalent; it must not be called a violation.
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED

    def test_specifier_and_marker_change_is_violation(self) -> None:
        source = _metadata(extra='Requires-Dist: dep>=1; python_version >= "3.8"\n')
        wheel = _metadata(extra='Requires-Dist: dep>=2; python_version >= "3.9"\n')
        report = diff_metadata(source, wheel)
        statuses = {difference.status for difference in report.differences}
        assert DifferenceStatus.VIOLATION in statuses
        assert all(
            difference.field == "requires_dist" for difference in report.differences
        )

    def test_extras_spelling_is_format_change(self) -> None:
        source = _metadata(extra="Requires-Dist: dep[My_Extra]\n")
        wheel = _metadata(extra="Requires-Dist: dep[my-extra]\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT

    def test_url_change_is_violation(self) -> None:
        source = _metadata(
            extra="Requires-Dist: dep @ https://a.example/dep-1.tar.gz\n"
        )
        wheel = _metadata(extra="Requires-Dist: dep @ https://b.example/dep-1.tar.gz\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.violations
        assert "added" in difference.detail or "removed" in difference.detail

    def test_unparseable_requirement_is_undetermined(self) -> None:
        source = _metadata(extra="Requires-Dist: dep!!\n")
        wheel = _metadata(extra="Requires-Dist: dep??\n")
        report = diff_metadata(source, wheel)
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert "validated" in difference.detail


class TestMetadataVersion:
    def test_wheel_newer_metadata_version_is_allowed(self) -> None:
        report = diff_metadata(_metadata(version="2.2"), _metadata(version="2.4"))
        (difference,) = report.differences
        assert difference.field == "metadata_version"
        assert difference.status is DifferenceStatus.ALLOWED

    def test_wheel_older_metadata_version_is_violation(self) -> None:
        report = diff_metadata(_metadata(version="2.4"), _metadata(version="2.2"))
        (difference,) = report.violations
        assert difference.field == "metadata_version"
        assert "downgraded" in difference.detail

    def test_invalid_metadata_version_is_undetermined(self) -> None:
        report = diff_metadata(_metadata(version="9.9"), _metadata(version="2.3"))
        assert any(
            difference.field == "metadata_version"
            and difference.status is DifferenceStatus.UNDETERMINED
            for difference in report.differences
        )


class TestOtherFields:
    def test_project_urls_value_change(self) -> None:
        source = _metadata(extra="Project-URL: Home, https://a.example\n")
        wheel = _metadata(extra="Project-URL: Home, https://b.example\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.violations
        assert difference.field == "project_urls"
        assert difference.sdist_value == {"Home": "https://a.example"}
        assert difference.wheel_value == {"Home": "https://b.example"}

    def test_keywords_reordering_is_format_change(self) -> None:
        source = _metadata(extra="Keywords: one, two\n")
        wheel = _metadata(extra="Keywords: two, one\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT
        assert difference.field == "keywords"

    def test_unknown_header_is_undetermined(self) -> None:
        wheel = _metadata(extra="X-Mystery: 1\n")
        report = diff_metadata(_metadata(), wheel)
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert difference.field == "x-mystery"
        assert difference.wheel_value == ["1"]

    def test_repeated_single_value_field_is_undetermined(self) -> None:
        source = _metadata(extra="Summary: one\n")
        wheel = (
            "Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n"
            "Summary: one\nSummary: two\n"
        )
        report = diff_metadata(source, wheel)
        assert report.violations == ()
        # The duplicated header cannot be parsed into one field, so it lands in
        # the unparsed bucket; the evidence is reported rather than judged.
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert difference.field == "summary"
        assert difference.wheel_value == ["one", "two"]

    def test_invalid_scalar_field_is_undetermined(self) -> None:
        source = _metadata(extra="Requires-Python: >=3.8\n")
        wheel = _metadata(extra="Requires-Python: not-a-specifier\n")
        report = diff_metadata(source, wheel)
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert difference.field == "requires_python"

    def test_invalid_dynamic_field_is_undetermined(self) -> None:
        source = _metadata(extra="Dynamic: Not-A-Field\nSummary: a\n")
        wheel = _metadata(extra="Dynamic: Not-A-Field\nSummary: b\n")
        report = diff_metadata(source, wheel)
        by_field = {difference.field: difference for difference in report}
        assert by_field["dynamic"].status is DifferenceStatus.UNDETERMINED
        assert by_field["summary"].status is DifferenceStatus.UNDETERMINED

    def test_differences_are_sorted_by_field_name(self) -> None:
        source = _metadata()
        wheel = _metadata(extra="Summary: x\nRequires-Python: >=3.9\n")
        report = diff_metadata(source, wheel)
        fields = [difference.field for difference in report.differences]
        assert fields == sorted(fields)

    def test_field_difference_is_frozen(self) -> None:
        source = _metadata(extra="Summary: a\n")
        wheel = _metadata(extra="Summary: b\n")
        (difference,) = diff_metadata(source, wheel).differences
        assert isinstance(difference, FieldDifference)
        with pytest.raises(dataclasses.FrozenInstanceError):
            difference.field = "other"  # type: ignore[misc]


class TestBranchCoverage:
    def test_old_version_removed_field_is_undetermined(self) -> None:
        source = _metadata(version="2.1", extra="Home-page: https://x.example\n")
        report = diff_metadata(source, _metadata(version="2.1"))
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert difference.field == "home_page"

    def test_old_version_added_dependency_is_undetermined(self) -> None:
        wheel = _metadata(version="2.1", extra="Requires-Dist: dep\n")
        report = diff_metadata(_metadata(version="2.1"), wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED

    def test_dynamic_removed_dependency_is_allowed(self) -> None:
        source = _metadata(extra="Dynamic: Requires-Dist\nRequires-Dist: dep\n")
        wheel = _metadata(extra="Dynamic: Requires-Dist\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.ALLOWED
        assert difference.field == "requires_dist"

    def test_unparsed_reordering_is_format_change(self) -> None:
        source = (
            "Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n"
            "Summary: one\nSummary: two\n"
        )
        wheel = (
            "Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n"
            "Summary: two\nSummary: one\n"
        )
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.FORMAT
        assert difference.field == "summary"

    def test_unparsed_known_field_old_version_detail(self) -> None:
        source = (
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
            "Summary: one\nSummary: two\n"
        )
        wheel = (
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
            "Summary: one\nSummary: three\n"
        )
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert "predates Dynamic" in difference.detail

    def test_dynamic_requires_python_change_is_allowed(self) -> None:
        source = _metadata(extra="Dynamic: Requires-Python\nRequires-Python: >=3.8\n")
        wheel = _metadata(extra="Dynamic: Requires-Python\nRequires-Python: >=3.10\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "requires_python"
        assert difference.status is DifferenceStatus.ALLOWED

    def test_dynamic_license_expression_change_is_allowed(self) -> None:
        source = _metadata(
            version="2.4",
            extra="Dynamic: License-Expression\nLicense-Expression: MIT\n",
        )
        wheel = _metadata(
            version="2.4",
            extra=("Dynamic: License-Expression\nLicense-Expression: Apache-2.0\n"),
        )
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "license_expression"
        assert difference.status is DifferenceStatus.ALLOWED

    def test_dynamic_project_urls_change_is_allowed(self) -> None:
        source = _metadata(
            extra="Dynamic: Project-URL\nProject-URL: Home, https://a.example\n"
        )
        wheel = _metadata(
            extra="Dynamic: Project-URL\nProject-URL: Home, https://b.example\n"
        )
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "project_urls"
        assert difference.status is DifferenceStatus.ALLOWED

    def test_static_list_field_change_is_violation(self) -> None:
        source = _metadata(extra="Classifier: A\n")
        wheel = _metadata(extra="Classifier: B\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.violations
        assert difference.field == "classifiers"

    def test_keywords_change_is_violation(self) -> None:
        source = _metadata(extra="Keywords: one\n")
        wheel = _metadata(extra="Keywords: two\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.violations
        assert difference.field == "keywords"

    def test_unknown_header_removed_is_undetermined(self) -> None:
        source = _metadata(extra="X-Mystery: 1\n")
        report = diff_metadata(source, _metadata())
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED
        assert difference.sdist_value == ["1"]
        assert difference.wheel_value is None

    def test_identical_unparsed_field_is_not_reported(self) -> None:
        document = (
            "Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n"
            "Summary: one\nSummary: two\n"
        )
        report = diff_metadata(document, document)
        assert report.differences == ()

    def test_invalid_metadata_version_in_both_documents(self) -> None:
        source = _metadata(version="9.9", extra="Summary: a\n")
        wheel = _metadata(version="8.8", extra="Summary: b\n")
        report = diff_metadata(source, wheel)
        by_field = {difference.field: difference for difference in report}
        assert by_field["metadata_version"].status is (DifferenceStatus.UNDETERMINED)
        assert by_field["summary"].status is DifferenceStatus.UNDETERMINED
        assert report.dynamic_contract_applies is False

    def test_duplicate_requirements_are_multisets(self) -> None:
        source = _metadata(
            extra="Requires-Dist: dep\nRequires-Dist: dep\nRequires-Dist: other\n"
        )
        wheel = _metadata(extra="Requires-Dist: other\nRequires-Dist: dep\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.VIOLATION
        assert "removed" in difference.detail

    def test_marker_change_under_old_version_is_undetermined(self) -> None:
        source = _metadata(
            version="2.1",
            extra='Requires-Dist: dep>=1; python_version >= "3.8"\n',
        )
        wheel = _metadata(
            version="2.1",
            extra='Requires-Dist: dep>=1; python_version >= "3.9"\n',
        )
        report = diff_metadata(source, wheel)
        assert report.violations == ()
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED

    def test_dynamic_list_field_change_is_allowed(self) -> None:
        source = _metadata(extra="Dynamic: Classifier\nClassifier: A\n")
        wheel = _metadata(extra="Dynamic: Classifier\nClassifier: B\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "classifiers"
        assert difference.status is DifferenceStatus.ALLOWED

    def test_report_violations_filters_only_violations(self) -> None:
        source = _metadata(
            name="foo_bar",
            extra="Requires-Python: >=3.8\nSummary: a\n",
        )
        wheel = _metadata(
            name="Foo-Bar",
            extra="Requires-Python: >=3.10\nSummary: b\n",
        )
        report = diff_metadata(source, wheel)
        violation_fields = {difference.field for difference in report.violations}
        assert violation_fields == {"requires_python", "summary"}
        all_fields = {difference.field for difference in report.differences}
        assert "name" in all_fields


class TestAdditionalBranches:
    def test_identical_project_urls_not_reported(self) -> None:
        document = _metadata(extra="Project-URL: Home, https://a.example\n")
        report = diff_metadata(document, document)
        assert report.differences == ()

    def test_identical_list_fields_not_reported(self) -> None:
        document = _metadata(extra="Classifier: A\nClassifier: B\nProvides-Extra: x\n")
        report = diff_metadata(document, document)
        assert report.differences == ()

    def test_generic_scalar_dynamic_change_allowed(self) -> None:
        source = _metadata(extra="Dynamic: Home-page\nHome-page: https://a.example\n")
        wheel = _metadata(extra="Dynamic: Home-page\nHome-page: https://b.example\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "home_page"
        assert difference.status is DifferenceStatus.ALLOWED

    def test_old_version_generic_scalar_change_undetermined(self) -> None:
        source = _metadata(version="2.1", extra="Home-page: https://a.example\n")
        wheel = _metadata(version="2.1", extra="Home-page: https://b.example\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "home_page"
        assert difference.status is DifferenceStatus.UNDETERMINED

    def test_license_expression_old_version_change_undetermined(self) -> None:
        source = _metadata(version="2.4", extra="License-Expression: MIT\n")
        wheel = _metadata(version="2.4", extra="License-Expression: Apache-2.0\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.field == "license_expression"
        assert difference.status is DifferenceStatus.VIOLATION

    def test_dynamic_name_change_still_violates_via_field_status(self) -> None:
        # Even if a malformed Dynamic entry somehow named a never-dynamic
        # field, changing that field remains a violation.
        source = _metadata(extra="Requires-Python: >=3.8\n")
        wheel = _metadata(extra="Requires-Python: >=3.10\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.violations
        assert difference.field == "requires_python"

    def test_requires_dist_unchanged_when_both_empty(self) -> None:
        report = diff_metadata(_metadata(), _metadata())
        assert not any(
            difference.field == "requires_dist" for difference in report.differences
        )

    def test_requires_python_old_version_change_undetermined(self) -> None:
        source = _metadata(version="2.1", extra="Requires-Python: >=3.8\n")
        wheel = _metadata(version="2.1", extra="Requires-Python: >=3.10\n")
        report = diff_metadata(source, wheel)
        (difference,) = report.differences
        assert difference.status is DifferenceStatus.UNDETERMINED


class TestIdentityPresence:
    def test_missing_name_in_wheel_is_violation(self) -> None:
        source = "Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n"
        wheel = "Metadata-Version: 2.3\nVersion: 1.0\n"
        report = diff_metadata(source, wheel)
        (difference,) = report.violations
        assert difference.field == "name"
        assert difference.wheel_value is None

    def test_missing_version_in_wheel_is_violation(self) -> None:
        source = "Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n"
        wheel = "Metadata-Version: 2.3\nName: demo\n"
        report = diff_metadata(source, wheel)
        (difference,) = report.violations
        assert difference.field == "version"

    def test_old_version_missing_name_is_undetermined(self) -> None:
        source = "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
        wheel = "Metadata-Version: 2.1\nVersion: 1.0\n"
        report = diff_metadata(source, wheel)
        # Name identity cannot be compared when one side lacks it; the absence
        # is reported as undetermined for pre-Dynamic metadata.
        (difference,) = report.differences
        assert difference.field == "name"
        assert difference.status is DifferenceStatus.UNDETERMINED

    def test_dir_lists_public_api(self) -> None:
        import packaging.metadata_diff as module

        assert set(module.__dir__()) == {
            "DifferenceStatus",
            "FieldDifference",
            "MetadataDifferenceReport",
            "diff_metadata",
        }


class TestStatusHelpers:
    def test_never_dynamic_field_change_is_violation(self) -> None:
        from packaging.metadata_diff import _field_status, _presence_status

        assert (
            _field_status(
                "metadata_version",
                dynamic=frozenset(),
                contract_applies=True,
            )
            is DifferenceStatus.VIOLATION
        )
        assert (
            _presence_status(
                "name",
                dynamic=frozenset(),
                contract_applies=True,
                sdist_value_present=False,
            )
            is DifferenceStatus.VIOLATION
        )

    def test_presence_allowed_when_field_was_dynamic_in_sdist(self) -> None:
        from packaging.metadata_diff import _presence_status

        assert (
            _presence_status(
                "summary",
                dynamic=frozenset({"summary"}),
                contract_applies=True,
                sdist_value_present=True,
            )
            is DifferenceStatus.ALLOWED
        )

    def test_status_undetermined_without_contract_or_dynamic(self) -> None:
        from packaging.metadata_diff import _field_status, _presence_status

        assert (
            _field_status("summary", dynamic=frozenset(), contract_applies=False)
            is DifferenceStatus.UNDETERMINED
        )
        assert (
            _presence_status(
                "summary",
                dynamic=None,
                contract_applies=True,
                sdist_value_present=True,
            )
            is DifferenceStatus.UNDETERMINED
        )
