# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import annotations

import pytest

from packaging.markers import Marker
from packaging.merge import (
    ConflictKind,
    MergedRequirement,
    SourcedRequirement,
    TargetEnvironment,
    merge_requirements,
)
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

WINDOWS = TargetEnvironment("windows", {"sys_platform": "win32"})
LINUX = TargetEnvironment("linux", {"sys_platform": "linux"})
MACOS = TargetEnvironment("macos", {"sys_platform": "darwin"})


def parse(line: str, source: str) -> SourcedRequirement:
    return SourcedRequirement.parse(line, source)


class TestSourcedRequirement:
    def test_parse_preserves_line_and_source(self) -> None:
        line = "Foo_Bar>=1.0; sys_platform == 'win32'"
        sourced = parse(line, "reqs/base.txt:3")

        assert sourced.requirement == Requirement(line)
        assert sourced.line == line
        assert sourced.source == "reqs/base.txt:3"


class TestMarkerEvaluationPerEnvironment:
    def test_windows_only_requirement(self) -> None:
        report = merge_requirements(
            [parse("foo; sys_platform == 'win32'", "reqs.txt:1")],
            [WINDOWS, LINUX],
        )

        windows = report.for_environment("windows")
        assert [str(r) for r in windows.requirements] == ["foo"]
        assert report.for_environment("linux").requirements == ()

    def test_platform_matrix_cross_check(self) -> None:
        # Every requirement's marker is evaluated independently per
        # environment; the merged names must match a direct re-evaluation
        # of the original requirements in each environment.
        lines = [
            "alpha; sys_platform == 'win32'",
            "beta>=1.0; sys_platform != 'win32'",
            "gamma; python_version >= '3.10'",
            "delta; sys_platform == 'win32' or sys_platform == 'darwin'",
        ]
        requirements = [parse(line, f"reqs.txt:{i}") for i, line in enumerate(lines, 1)]
        environments = [WINDOWS, LINUX, MACOS]

        report = merge_requirements(requirements, environments)

        for env in environments:
            expected = sorted(
                {
                    canonicalize_name(Requirement(line).name)
                    for line in lines
                    if Marker(line.split(";", 1)[1].strip()).evaluate(env.environment)
                }
            )
            group = report.for_environment(env.label)
            assert [r.name for r in group.requirements] == expected

    def test_extra_marker(self) -> None:
        requirements = [
            parse("tool; extra == 'test'", "project[test]:1"),
            parse("base", "project:1"),
        ]
        environments = [
            TargetEnvironment("plain", {"extra": ""}),
            TargetEnvironment("with-test", {"extra": "test"}),
        ]

        report = merge_requirements(requirements, environments)

        plain = report.for_environment("plain")
        assert [str(r) for r in plain.requirements] == ["base"]
        assert [str(r) for r in report.for_environment("with-test").requirements] == [
            "base",
            "tool",
        ]

    def test_extra_marker_value_is_normalized(self) -> None:
        requirements = [parse("tool; extra == 'Te.st'", "project:1")]
        environments = [TargetEnvironment("env", {"extra": "te-st"})]

        report = merge_requirements(requirements, environments)

        assert [r.name for r in report.groups[0].requirements] == ["tool"]


class TestMerging:
    def test_duplicate_spellings_merge_into_one_entry(self) -> None:
        requirements = [
            parse("Foo_Bar[zip]>=1.0", "a.txt:1"),
            parse("foo-bar[yaml]<3", "b.txt:2"),
            parse("foo.bar", "c.txt:3"),
        ]

        report = merge_requirements(requirements, [LINUX])

        (merged,) = report.groups[0].requirements
        assert merged.name == "foo-bar"
        assert merged.extras == frozenset({"zip", "yaml"})
        assert merged.specifier == SpecifierSet(">=1.0,<3")
        assert [s.source for s in merged.sources] == ["a.txt:1", "b.txt:2", "c.txt:3"]

    def test_version_ranges_intersect(self) -> None:
        requirements = [
            parse("foo>=1.0,!=1.2", "a.txt:1"),
            parse("foo<2.0", "b.txt:1"),
        ]

        report = merge_requirements(requirements, [LINUX])

        (merged,) = report.groups[0].requirements
        assert merged.specifier == SpecifierSet(">=1.0,!=1.2,<2.0")
        assert str(merged) == "foo!=1.2,<2.0,>=1.0"

    def test_prerelease_specifiers_merge(self) -> None:
        requirements = [
            parse("foo>=1.0a1", "a.txt:1"),
            parse("foo<2", "b.txt:1"),
        ]

        report = merge_requirements(requirements, [LINUX])

        (merged,) = report.groups[0].requirements
        assert merged.specifier == SpecifierSet(">=1.0a1,<2")
        assert merged.specifier.contains("1.0b1")
        assert not report.groups[0].conflicts

    def test_prerelease_only_intersection_is_unsatisfiable(self) -> None:
        # <2 excludes the whole 2.0 line (2.0.dev0 bound), so nothing can
        # satisfy >=2.0a1,<2.
        requirements = [
            parse("foo>=2.0a1", "a.txt:1"),
            parse("foo<2", "b.txt:1"),
        ]

        report = merge_requirements(requirements, [LINUX])

        (conflict,) = report.groups[0].conflicts
        assert conflict.kind is ConflictKind.UNSATISFIABLE_SPECIFIERS
        assert conflict.name == "foo"
        assert [s.source for s in conflict.sources] == ["a.txt:1", "b.txt:1"]
        assert report.groups[0].requirements == ()

    def test_unsatisfiable_intersection_reports_sources(self) -> None:
        requirements = [
            parse("foo<1.0", "a.txt:4"),
            parse("foo>=2.0", "b.txt:9"),
            parse("foo", "c.txt:1"),
        ]

        report = merge_requirements(requirements, [LINUX])

        (conflict,) = report.groups[0].conflicts
        assert conflict.kind is ConflictKind.UNSATISFIABLE_SPECIFIERS
        # The bare "foo" line carries no specifier and is not blamed.
        assert [s.source for s in conflict.sources] == ["a.txt:4", "b.txt:9"]
        assert [s.line for s in conflict.sources] == ["foo<1.0", "foo>=2.0"]
        assert "admits no version" in conflict.detail


class TestDirectUrls:
    def test_same_url_merges(self) -> None:
        requirements = [
            parse("foo @ https://example.com/foo-1.0.tar.gz", "a.txt:1"),
            parse("Foo[zip] @ https://example.com/foo-1.0.tar.gz", "b.txt:2"),
        ]

        report = merge_requirements(requirements, [LINUX])

        (merged,) = report.groups[0].requirements
        assert merged.url == "https://example.com/foo-1.0.tar.gz"
        assert merged.extras == frozenset({"zip"})
        assert str(merged) == "foo[zip] @ https://example.com/foo-1.0.tar.gz"
        assert not report.groups[0].conflicts

    def test_conflicting_urls(self) -> None:
        requirements = [
            parse("foo @ https://example.com/foo-1.0.tar.gz", "a.txt:1"),
            parse("foo @ https://mirror.example.com/foo-1.0.tar.gz", "b.txt:2"),
        ]

        report = merge_requirements(requirements, [LINUX])

        (conflict,) = report.groups[0].conflicts
        assert conflict.kind is ConflictKind.CONFLICTING_URLS
        assert [s.source for s in conflict.sources] == ["a.txt:1", "b.txt:2"]
        assert "https://example.com/foo-1.0.tar.gz" in conflict.detail
        assert "https://mirror.example.com/foo-1.0.tar.gz" in conflict.detail
        assert report.groups[0].requirements == ()

    def test_url_vs_specifier_conflict(self) -> None:
        requirements = [
            parse("foo @ https://example.com/foo-1.0.tar.gz", "a.txt:1"),
            parse("foo>=1.0", "b.txt:2"),
        ]

        report = merge_requirements(requirements, [LINUX])

        (conflict,) = report.groups[0].conflicts
        assert conflict.kind is ConflictKind.URL_VS_SPECIFIER
        assert [s.source for s in conflict.sources] == ["a.txt:1", "b.txt:2"]

    def test_url_vs_constraint_specifier_conflict(self) -> None:
        requirements = [parse("foo @ https://example.com/foo-1.0.tar.gz", "a.txt:1")]
        constraints = [parse("foo==1.0", "constraints.txt:1")]

        report = merge_requirements(requirements, [LINUX], constraints)

        (conflict,) = report.groups[0].conflicts
        assert conflict.kind is ConflictKind.URL_VS_SPECIFIER
        assert [s.source for s in conflict.sources] == ["a.txt:1", "constraints.txt:1"]

    def test_url_conflict_only_in_environment_where_both_are_active(self) -> None:
        requirements = [
            parse(
                "foo @ https://example.com/foo-1.0.tar.gz ; sys_platform == 'win32'",
                "a.txt:1",
            ),
            parse("foo>=1.0; sys_platform == 'win32'", "b.txt:2"),
            parse("foo>=1.0; sys_platform != 'win32'", "b.txt:3"),
        ]

        report = merge_requirements(requirements, [WINDOWS, LINUX])

        windows = report.for_environment("windows")
        assert [c.kind for c in windows.conflicts] == [ConflictKind.URL_VS_SPECIFIER]
        linux = report.for_environment("linux")
        assert [str(r) for r in linux.requirements] == ["foo>=1.0"]
        assert not linux.conflicts


class TestConstraints:
    def test_constraint_tightens_activated_requirement(self) -> None:
        requirements = [parse("foo>=1.0", "reqs.txt:1")]
        constraints = [parse("foo<1.5", "constraints.txt:1")]

        report = merge_requirements(requirements, [LINUX], constraints)

        (merged,) = report.groups[0].requirements
        assert merged.specifier == SpecifierSet(">=1.0,<1.5")
        assert [s.source for s in merged.sources] == ["reqs.txt:1", "constraints.txt:1"]

    def test_constraint_alone_creates_no_install_item(self) -> None:
        report = merge_requirements(
            [parse("foo", "reqs.txt:1")],
            [LINUX],
            [parse("bar<2.0", "constraints.txt:1")],
        )

        assert [r.name for r in report.groups[0].requirements] == ["foo"]

    def test_constraint_marker_is_evaluated_per_environment(self) -> None:
        requirements = [parse("foo>=1.0", "reqs.txt:1")]
        constraints = [parse("foo<1.5; sys_platform == 'win32'", "constraints.txt:1")]

        report = merge_requirements(requirements, [WINDOWS, LINUX], constraints)

        (win,) = report.for_environment("windows").requirements
        assert win.specifier == SpecifierSet(">=1.0,<1.5")
        (nix,) = report.for_environment("linux").requirements
        assert nix.specifier == SpecifierSet(">=1.0")

    def test_constraint_can_trigger_unsatisfiable_conflict(self) -> None:
        requirements = [parse("foo>=2.0", "reqs.txt:1")]
        constraints = [parse("foo<1.0", "constraints.txt:1")]

        report = merge_requirements(requirements, [LINUX], constraints)

        (conflict,) = report.groups[0].conflicts
        assert conflict.kind is ConflictKind.UNSATISFIABLE_SPECIFIERS
        assert [s.source for s in conflict.sources] == [
            "reqs.txt:1",
            "constraints.txt:1",
        ]


class TestEnvironmentGrouping:
    def test_equivalent_environments_share_one_group(self) -> None:
        requirements = [parse("foo; os_name == 'posix'", "reqs.txt:1")]
        environments = [
            TargetEnvironment("linux", {"os_name": "posix", "platform_release": "6.5"}),
            TargetEnvironment(
                "macos", {"os_name": "posix", "platform_release": "23.0"}
            ),
            TargetEnvironment("windows", {"os_name": "nt"}),
        ]

        report = merge_requirements(requirements, environments)

        assert len(report.groups) == 2
        posix = report.for_environment("macos")
        assert posix.environments == ("linux", "macos")
        assert [str(r) for r in posix.requirements] == ["foo"]
        assert report.for_environment("windows").requirements == ()

    def test_groups_do_not_synthesize_markers(self) -> None:
        # Grouping is by proven equivalence only: a group carries the exact
        # input labels and no marker expression claiming wider coverage.
        requirements = [parse("foo>=1.0", "reqs.txt:1")]
        environments = [
            TargetEnvironment("py312", {"python_version": "3.12"}),
            TargetEnvironment("py313", {"python_version": "3.13"}),
        ]

        report = merge_requirements(requirements, environments)

        (group,) = report.groups
        assert group.environments == ("py312", "py313")
        assert not hasattr(group, "marker")

    def test_for_environment_unknown_label(self) -> None:
        report = merge_requirements([parse("foo", "reqs.txt:1")], [LINUX])

        with pytest.raises(KeyError):
            report.for_environment("windows")

    def test_no_environments_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one target environment"):
            merge_requirements([parse("foo", "reqs.txt:1")], [])

    def test_duplicate_environment_labels_rejected(self) -> None:
        with pytest.raises(ValueError, match="labels must be unique"):
            merge_requirements(
                [parse("foo", "reqs.txt:1")],
                [
                    TargetEnvironment("linux", {"sys_platform": "linux"}),
                    TargetEnvironment("linux", {"sys_platform": "linux2"}),
                ],
            )


class TestMergedRequirementStr:
    @pytest.mark.parametrize(
        ("merged", "expected"),
        [
            (
                MergedRequirement(
                    name=canonicalize_name("Foo"),
                    specifier=SpecifierSet(">=1.0,<3"),
                    extras=frozenset({"yaml", "zip"}),
                    url=None,
                    sources=(),
                ),
                "foo[yaml,zip]<3,>=1.0",
            ),
            (
                MergedRequirement(
                    name=canonicalize_name("foo"),
                    specifier=SpecifierSet(""),
                    extras=frozenset(),
                    url="https://example.com/foo.tar.gz",
                    sources=(),
                ),
                "foo @ https://example.com/foo.tar.gz",
            ),
            (
                MergedRequirement(
                    name=canonicalize_name("foo"),
                    specifier=SpecifierSet(""),
                    extras=frozenset(),
                    url=None,
                    sources=(),
                ),
                "foo",
            ),
        ],
    )
    def test_render(self, merged: MergedRequirement, expected: str) -> None:
        assert str(merged) == expected
