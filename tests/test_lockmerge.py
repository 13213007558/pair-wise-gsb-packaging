# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import annotations

import pytest

from packaging.lockmerge import (
    InputError,
    LineEvaluation,
    MergedRequirement,
    MergeReport,
    RequirementLine,
    TargetEnvironment,
    merge_requirements,
)
from packaging.markers import UndefinedEnvironmentName


def make_environment(
    name: str,
    extras: tuple[str, ...] = (),
    **overrides: str,
) -> TargetEnvironment:
    values = {
        "implementation_name": "cpython",
        "implementation_version": "3.12.0",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "6.5.0",
        "platform_system": "Linux",
        "platform_version": "#1 SMP",
        "python_full_version": "3.12.0",
        "python_version": "3.12",
        "sys_platform": "linux",
    }
    values.update(overrides)
    return TargetEnvironment(name=name, values=values, extras=frozenset(extras))


WINDOWS = make_environment("windows", sys_platform="win32", os_name="nt")
LINUX = make_environment("linux")
LINUX_TEST = make_environment("linux-test", extras=("test",))


def plan_requirements(
    report: MergeReport, environment: str
) -> dict[str, MergedRequirement]:
    return {str(req): req for req in report.plan_for(environment).requirements}


class TestMerging:
    def test_marker_evaluated_before_merging(self) -> None:
        report = merge_requirements(
            [
                RequirementLine('foo>=1.0; sys_platform == "win32"', "win.txt"),
                RequirementLine("foo<3", "base.txt"),
            ],
            [WINDOWS, LINUX],
        )

        windows = plan_requirements(report, "windows")
        assert list(windows) == ["foo<3,>=1.0"]
        assert [line.source for line in windows["foo<3,>=1.0"].lines] == [
            "win.txt",
            "base.txt",
        ]

        linux = plan_requirements(report, "linux")
        assert list(linux) == ["foo<3"]
        assert [line.source for line in linux["foo<3"].lines] == ["base.txt"]

    def test_extras_are_merged_and_normalized(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo[Bar_Baz]", "a.txt"),
                RequirementLine("foo[quux]", "b.txt"),
            ],
            [LINUX],
        )
        (req,) = report.plan_for("linux").requirements
        assert req.extras == ("bar-baz", "quux")
        assert str(req) == "foo[bar-baz,quux]"

    def test_duplicate_project_spellings_merge(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("Foo_Bar>=1.0", "a.txt"),
                RequirementLine("foo-bar<3", "b.txt"),
                RequirementLine("FOO.bar[baz]", "c.txt"),
            ],
            [LINUX],
        )
        (req,) = report.plan_for("linux").requirements
        assert req.name == "foo-bar"
        assert str(req) == "foo-bar[baz]<3,>=1.0"
        assert [line.source for line in req.lines] == ["a.txt", "b.txt", "c.txt"]

    def test_prerelease_specifiers_merge(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo>=2.0a1", "a.txt"),
                RequirementLine("foo<3", "b.txt"),
            ],
            [LINUX],
        )
        (req,) = report.plan_for("linux").requirements
        assert str(req.specifier) == "<3,>=2.0a1"
        # The merged specifier still admits pre-releases.
        assert req.specifier.contains("2.0b1")

    def test_direct_url_merges_with_bare_requirement(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo", "a.txt"),
                RequirementLine("foo[zip] @ https://example.com/foo.zip", "b.txt"),
            ],
            [LINUX],
        )
        (req,) = report.plan_for("linux").requirements
        assert req.url == "https://example.com/foo.zip"
        assert str(req) == "foo[zip] @ https://example.com/foo.zip"

    def test_same_direct_url_from_requirement_and_constraint(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo @ https://example.com/foo.zip", "a.txt"),
                RequirementLine(
                    "foo @ https://example.com/foo.zip",
                    "constraints.txt",
                    constraint=True,
                ),
            ],
            [LINUX],
        )
        (req,) = report.plan_for("linux").requirements
        assert req.url == "https://example.com/foo.zip"
        assert [line.source for line in req.lines] == ["a.txt", "constraints.txt"]

    def test_plain_requirement(self) -> None:
        report = merge_requirements([RequirementLine("foo", "a.txt")], [LINUX])
        (req,) = report.plan_for("linux").requirements
        assert str(req) == "foo"
        assert not req.specifier
        assert req.url is None
        assert req.extras == ()


class TestExtraMarkers:
    def test_extra_marker_selects_requested_extra(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo>=1", "base.txt"),
                RequirementLine('foo[test]; extra == "test"', "extras.txt"),
            ],
            [LINUX, LINUX_TEST],
        )

        linux = plan_requirements(report, "linux")
        assert [line.source for line in linux["foo>=1"].lines] == ["base.txt"]

        linux_test = plan_requirements(report, "linux-test")
        assert [line.source for line in linux_test["foo[test]>=1"].lines] == [
            "base.txt",
            "extras.txt",
        ]

    def test_extra_marker_value_is_normalized(self) -> None:
        report = merge_requirements(
            [RequirementLine('foo; extra == "Test"', "extras.txt")],
            [make_environment("env", extras=("t-est",))],
        )
        # "Test" normalizes to "test", which does not match "t-est".
        assert report.plan_for("env").requirements == ()

        report = merge_requirements(
            [RequirementLine('foo; extra == "Test"', "extras.txt")],
            [make_environment("env", extras=("test",))],
        )
        assert [str(req) for req in report.plan_for("env").requirements] == ["foo"]

    def test_extra_marker_reversed_operands(self) -> None:
        report = merge_requirements(
            [RequirementLine('foo; "test" == extra', "extras.txt")],
            [LINUX, LINUX_TEST],
        )
        assert report.plan_for("linux").requirements == ()
        assert [str(req) for req in report.plan_for("linux-test").requirements] == [
            "foo"
        ]

    def test_extra_marker_negated(self) -> None:
        report = merge_requirements(
            [RequirementLine('foo; extra != "test"', "extras.txt")],
            [LINUX, LINUX_TEST],
        )
        assert [str(req) for req in report.plan_for("linux").requirements] == ["foo"]
        assert report.plan_for("linux-test").requirements == ()

    def test_extra_marker_combined_with_platform(self) -> None:
        report = merge_requirements(
            [
                RequirementLine(
                    'foo; extra == "test" and '
                    '(sys_platform == "linux" or sys_platform == "win32")',
                    "extras.txt",
                )
            ],
            [LINUX, LINUX_TEST, WINDOWS],
        )
        assert report.plan_for("linux").requirements == ()
        assert report.plan_for("windows").requirements == ()
        assert [str(req) for req in report.plan_for("linux-test").requirements] == [
            "foo"
        ]

    def test_extra_marker_unusual_operator_uses_empty_extra(self) -> None:
        report = merge_requirements(
            [RequirementLine('foo; extra > "a"', "extras.txt")],
            [LINUX_TEST],
        )
        # ">" is not a set-membership test; the comparison sees an empty
        # extra, and "" > "a" is false.
        assert report.plan_for("linux-test").requirements == ()


class TestConstraints:
    def test_constraint_does_not_activate_package(self) -> None:
        report = merge_requirements(
            [RequirementLine("lonely<2", "constraints.txt", constraint=True)],
            [LINUX],
        )
        plan = report.plan_for("linux")
        assert plan.requirements == ()
        assert plan.conflicts == ()
        (evaluation,) = report.evaluations
        assert evaluation.active
        assert not evaluation.applied

    def test_constraint_narrows_activated_package(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo>=1", "base.txt"),
                RequirementLine("foo<2", "constraints.txt", constraint=True),
            ],
            [LINUX],
        )
        (req,) = report.plan_for("linux").requirements
        assert str(req) == "foo<2,>=1"
        assert [line.source for line in req.lines] == ["base.txt", "constraints.txt"]

    def test_constraint_marker_is_respected(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo", "base.txt"),
                RequirementLine(
                    'foo<2; sys_platform == "win32"',
                    "constraints.txt",
                    constraint=True,
                ),
            ],
            [WINDOWS, LINUX],
        )
        assert list(plan_requirements(report, "windows")) == ["foo<2"]
        assert list(plan_requirements(report, "linux")) == ["foo"]

    def test_constraint_extras_are_ignored(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo", "base.txt"),
                RequirementLine("foo[bar]>=1", "constraints.txt", constraint=True),
            ],
            [LINUX],
        )
        (req,) = report.plan_for("linux").requirements
        assert req.extras == ()
        assert str(req) == "foo>=1"


class TestConflicts:
    def test_url_and_specifier_conflict(self) -> None:
        lines = [
            RequirementLine("foo @ https://a.example/foo.zip", "a.txt"),
            RequirementLine("foo>=1", "b.txt"),
        ]
        report = merge_requirements(lines, [LINUX])
        plan = report.plan_for("linux")
        assert plan.requirements == ()
        (conflict,) = plan.conflicts
        assert conflict.kind == "url-with-specifier"
        assert conflict.name == "foo"
        assert conflict.lines == tuple(lines)
        assert "a.txt" in conflict.message
        assert "b.txt" in conflict.message

    def test_conflicting_urls(self) -> None:
        lines = [
            RequirementLine("foo @ https://a.example/foo.zip", "a.txt"),
            RequirementLine("foo @ https://b.example/foo.zip", "b.txt"),
        ]
        report = merge_requirements(lines, [LINUX])
        (conflict,) = report.plan_for("linux").conflicts
        assert conflict.kind == "conflicting-urls"
        assert conflict.lines == tuple(lines)

    def test_unsatisfiable_specifier(self) -> None:
        lines = [
            RequirementLine("foo>=2", "a.txt"),
            RequirementLine("foo<1", "b.txt"),
        ]
        report = merge_requirements(lines, [LINUX])
        plan = report.plan_for("linux")
        assert plan.requirements == ()
        (conflict,) = plan.conflicts
        assert conflict.kind == "unsatisfiable-specifier"
        assert conflict.lines == tuple(lines)
        assert "<1,>=2" in conflict.message

    def test_unsatisfiable_prerelease_intersection(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("foo>1.0", "a.txt"),
                RequirementLine("foo<1.0a1", "b.txt"),
            ],
            [LINUX],
        )
        (conflict,) = report.plan_for("linux").conflicts
        assert conflict.kind == "unsatisfiable-specifier"

    def test_conflicts_are_per_environment(self) -> None:
        report = merge_requirements(
            [
                RequirementLine(
                    'foo @ https://a.example/foo.zip ; sys_platform == "win32"',
                    "win.txt",
                ),
                RequirementLine("foo>=1", "base.txt"),
            ],
            [WINDOWS, LINUX],
        )
        windows = report.plan_for("windows")
        assert windows.requirements == ()
        assert [c.kind for c in windows.conflicts] == ["url-with-specifier"]
        assert list(plan_requirements(report, "linux")) == ["foo>=1"]


class TestEquivalentEnvironments:
    def test_equivalent_environments_share_one_plan(self) -> None:
        linux_a = make_environment("linux-a")
        linux_b = make_environment("linux-b")
        report = merge_requirements(
            [RequirementLine('foo>=1; sys_platform == "linux"', "base.txt")],
            [linux_a, linux_b, WINDOWS],
        )
        assert len(report.plans) == 2
        grouped = report.plan_for("linux-a")
        assert grouped.environments == ("linux-a", "linux-b")
        assert report.plan_for("linux-b") is grouped
        # The merged output carries no marker: it only ever names the
        # explicit environments it was proven to cover.
        (req,) = grouped.requirements
        assert not hasattr(req, "marker")
        assert ";" not in str(req)
        assert report.plan_for("windows").requirements == ()

    def test_different_results_are_not_grouped(self) -> None:
        report = merge_requirements(
            [RequirementLine('foo[test]; extra == "test"', "extras.txt")],
            [LINUX, LINUX_TEST],
        )
        assert len(report.plans) == 2


class TestEvaluations:
    def test_per_environment_line_evaluation(self) -> None:
        lines = [
            RequirementLine('Foo_Bar>=1.0; sys_platform == "win32"', "win.txt"),
            RequirementLine("foo-bar[baz]<3", "base.txt"),
            RequirementLine('foo-bar[qux]; extra == "test"', "extras.txt"),
            RequirementLine("foo-bar>=0.5", "constraints.txt", constraint=True),
            RequirementLine("lonely<2", "constraints.txt", constraint=True),
        ]
        report = merge_requirements(lines, [WINDOWS, LINUX_TEST])

        def expected(
            environment: str, states: list[tuple[bool, bool]]
        ) -> list[LineEvaluation]:
            return [
                LineEvaluation(
                    environment=environment,
                    line=line,
                    active=active,
                    applied=applied,
                )
                for line, (active, applied) in zip(lines, states, strict=True)
            ]

        assert report.evaluations == tuple(
            expected(
                "windows",
                [
                    (True, True),
                    (True, True),
                    (False, False),
                    (True, True),
                    (True, False),
                ],
            )
            + expected(
                "linux-test",
                [
                    (False, False),
                    (True, True),
                    (True, True),
                    (True, True),
                    (True, False),
                ],
            )
        )


class TestInputHandling:
    def test_invalid_lines_are_reported_and_skipped(self) -> None:
        report = merge_requirements(
            [
                RequirementLine("this is not a requirement", "bad.txt"),
                RequirementLine("foo", "base.txt"),
            ],
            [LINUX],
        )
        assert report.errors == (
            InputError(
                source="bad.txt",
                line="this is not a requirement",
                message=report.errors[0].message,
            ),
        )
        assert report.errors[0].message
        assert [str(req) for req in report.plan_for("linux").requirements] == ["foo"]
        assert [ev.line.line for ev in report.evaluations] == ["foo"]

    def test_duplicate_environment_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate target environment"):
            merge_requirements([], [LINUX, LINUX])

    def test_missing_marker_key_raises(self) -> None:
        env = TargetEnvironment(name="bare", values={"os_name": "posix"})
        with pytest.raises(UndefinedEnvironmentName):
            merge_requirements(
                [RequirementLine('foo; sys_platform == "linux"', "base.txt")],
                [env],
            )

    def test_environment_without_python_full_version(self) -> None:
        env = TargetEnvironment(name="bare", values={"sys_platform": "linux"})
        report = merge_requirements(
            [RequirementLine('foo; sys_platform == "linux"', "base.txt")],
            [env],
        )
        assert [str(req) for req in report.plan_for("bare").requirements] == ["foo"]

    def test_python_full_version_is_repaired(self) -> None:
        env = TargetEnvironment(name="local", values={"python_full_version": "3.12+"})
        report = merge_requirements(
            [RequirementLine('foo; python_full_version == "3.12+local"', "base.txt")],
            [env],
        )
        assert [str(req) for req in report.plan_for("local").requirements] == ["foo"]

    def test_plan_for_unknown_environment(self) -> None:
        report = merge_requirements([], [LINUX])
        with pytest.raises(KeyError):
            report.plan_for("windows")

    def test_empty_inputs(self) -> None:
        report = merge_requirements([], [LINUX])
        assert len(report.plans) == 1
        assert report.plans[0].requirements == ()
        assert report.evaluations == ()
        assert report.errors == ()

        assert merge_requirements([], []).plans == ()
