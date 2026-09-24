# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Merge requirement lines against an explicit environment matrix.

A lockfile service typically collects many requirement lines for the same
project: some only apply on one platform, some request extras, some pin
versions, and some are *constraints* that should only narrow down a package
that a real requirement already selects.  This module merges such lines
against a finite, explicitly listed set of target environments and reports,
per environment, the normalized constraint that actually applies.

The merge works in three steps:

1. Requirements are grouped by their normalized project name, so lines
   spelling the same project differently (``Foo_Bar`` vs ``foo-bar``)
   merge into a single group.
2. For each target environment every line's marker is evaluated first;
   only lines whose marker matches the environment take part in the merge.
   ``extra`` markers are resolved against the extras requested for
   that environment.
3. The surviving lines are merged: extras are unioned, version specifiers
   are intersected, and direct URLs must agree.  Conflicts (a direct URL
   combined with a version specifier, two different URLs, or an
   unsatisfiable specifier intersection) are reported per environment and
   point back to the original input lines and their source labels.

Constraints never *activate* a package: a constraint line only restricts a
package that at least one non-constraint requirement line selects in the
same environment.

Environments that resolve to exactly the same plan are grouped together in
the report.  The report never invents a new marker expression claiming to
cover environments beyond the ones it was given; it only ever names the
explicit environments that were proven equivalent.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Literal

from ._parser import Op, Value, Variable
from .markers import (
    Marker,
    _evaluate_markers,
    _repair_python_full_version,
)
from .requirements import InvalidRequirement, Requirement
from .specifiers import SpecifierSet
from .utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from collections.abc import Set as AbstractSet

    from ._parser import MarkerAtom, MarkerList, MarkerLogical
    from .utils import NormalizedName

__all__ = [
    "ConflictKind",
    "EnvironmentPlan",
    "InputError",
    "LineEvaluation",
    "MergeConflict",
    "MergeReport",
    "MergedRequirement",
    "RequirementLine",
    "TargetEnvironment",
    "merge_requirements",
]


def __dir__() -> list[str]:
    return __all__


ConflictKind = Literal[
    "conflicting-urls", "url-with-specifier", "unsatisfiable-specifier"
]
"""The kinds of merge conflict reported in :class:``MergeConflict``."""

_CONSTANT_KEY = "_packaging_lockmerge_constant"


@dataclasses.dataclass(frozen=True)
class RequirementLine:
    """A single requirement line together with its origin.

    :param line: The raw requirement line, e.g.
        ``'foo[bar]>=1.0; sys_platform == "win32"'``.
    :param source: A label identifying where the line came from (a file name,
        a requirements section, ...), preserved so applications can display
        conflicts against the original inputs.
    :param constraint: When true, the line is a constraint: it only narrows
        the version (or URL) of a package that a non-constraint line selects
        in the same environment, and never activates a package by itself.
    """

    line: str
    source: str
    constraint: bool = False


@dataclasses.dataclass(frozen=True)
class TargetEnvironment:
    """One explicitly listed target environment.

    :param name: A label for the environment, used in the report.
    :param values: The marker environment values (the keys of
        :func:``packaging.markers.default_environment``, e.g.
        ``sys_platform`` or ``python_version``).
    :param extras: The extras requested in this environment; ``extra``
        markers are resolved against this set.
    """

    name: str
    values: Mapping[str, str]
    extras: AbstractSet[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "extras", frozenset(canonicalize_name(e) for e in self.extras)
        )


@dataclasses.dataclass(frozen=True)
class InputError:
    """An input line that could not be parsed as a requirement."""

    source: str
    line: str
    message: str


@dataclasses.dataclass(frozen=True)
class MergedRequirement:
    """The normalized constraint one environment actually needs.

    ``lines`` lists the original :class:``RequirementLine``
    entries (in input order) that were merged into this constraint, so
    applications can show where each part came from.
    """

    name: NormalizedName
    extras: tuple[str, ...]
    specifier: SpecifierSet
    url: str | None
    lines: tuple[RequirementLine, ...]

    def __str__(self) -> str:
        text: str = self.name
        if self.extras:
            text += f"[{','.join(self.extras)}]"
        if self.url is not None:
            text += f" @ {self.url}"
        elif self.specifier:
            text += str(self.specifier)
        return text


@dataclasses.dataclass(frozen=True)
class MergeConflict:
    """A merge conflict, pointing back to the conflicting input lines."""

    name: NormalizedName
    kind: ConflictKind
    message: str
    lines: tuple[RequirementLine, ...]


@dataclasses.dataclass(frozen=True)
class LineEvaluation:
    """How one input line fared in one environment.

    ``active`` records whether the line's marker matched the
    environment.  ``applied`` records whether the line contributed
    to a merged requirement; it is false for inactive lines, for constraints
    whose package no requirement activated, and for lines involved in a
    conflict.
    """

    environment: str
    line: RequirementLine
    active: bool
    applied: bool


@dataclasses.dataclass(frozen=True)
class EnvironmentPlan:
    """The merged result shared by one or more equivalent environments.

    ``environments`` names exactly the target environments that were
    proven to resolve to this plan; it is never generalized into a marker
    expression covering unknown environments.
    """

    environments: tuple[str, ...]
    requirements: tuple[MergedRequirement, ...]
    conflicts: tuple[MergeConflict, ...]


@dataclasses.dataclass(frozen=True)
class MergeReport:
    """The outcome of :func:``merge_requirements``."""

    plans: tuple[EnvironmentPlan, ...]
    evaluations: tuple[LineEvaluation, ...]
    errors: tuple[InputError, ...]

    def plan_for(self, environment: str) -> EnvironmentPlan:
        """Return the plan covering the named target environment.

        :raises KeyError: If no environment with that name was merged.
        """
        for plan in self.plans:
            if environment in plan.environments:
                return plan
        raise KeyError(environment)


@dataclasses.dataclass(frozen=True)
class _ParsedLine:
    line: RequirementLine
    requirement: Requirement
    name: NormalizedName


def _constant_atom(value: bool) -> MarkerAtom:
    return (Variable(_CONSTANT_KEY), Op("=="), Value("1" if value else "0"))


def _resolve_extra_atom(
    atom: MarkerAtom | MarkerLogical, extras: AbstractSet[str]
) -> MarkerAtom | MarkerLogical:
    if not isinstance(atom, tuple):
        # "and" / "or" logical operators pass through unchanged.
        return atom
    lhs, op, rhs = atom
    if isinstance(lhs, Variable) and lhs.value == "extra" and isinstance(rhs, Value):
        compared = rhs.value
    elif isinstance(rhs, Variable) and rhs.value == "extra" and isinstance(lhs, Value):
        compared = lhs.value
    else:
        return atom
    # ``extra`` values are normalized when the marker is parsed, and
    # the target environment's extras are normalized by TargetEnvironment.
    if op.serialize() == "==":
        return _constant_atom(compared in extras)
    if op.serialize() == "!=":
        return _constant_atom(compared not in extras)
    return atom


def _substitute_extras(markers: MarkerList, extras: AbstractSet[str]) -> MarkerList:
    substituted: MarkerList = []
    for item in markers:
        if isinstance(item, list):
            substituted.append(_substitute_extras(item, extras))
        else:
            substituted.append(_resolve_extra_atom(item, extras))
    return substituted


def _marker_applies(marker: Marker | None, environment: TargetEnvironment) -> bool:
    if marker is None:
        return True
    values: dict[str, str | AbstractSet[str]] = dict(environment.values)
    values[_CONSTANT_KEY] = "1"
    # Any ``extra`` comparison not resolvable against the requested
    # extras (e.g. an exotic operator) sees the same empty extra that
    # Marker.evaluate defaults to.
    values["extra"] = ""
    if "python_full_version" in values:
        values = _repair_python_full_version(values)
    markers = _substitute_extras(marker._markers, environment.extras)
    return _evaluate_markers(markers, values)


def _format_lines(lines: Iterable[RequirementLine]) -> str:
    return ", ".join(f"{entry.line!r} (from {entry.source})" for entry in lines)


def _merge_group(
    name: NormalizedName, entries: list[_ParsedLine]
) -> MergedRequirement | MergeConflict:
    requirements = [entry for entry in entries if not entry.line.constraint]
    lines = tuple(entry.line for entry in entries)

    urls = {entry.requirement.url for entry in entries if entry.requirement.url}
    with_url = tuple(entry.line for entry in entries if entry.requirement.url)
    with_specifier = tuple(
        entry.line for entry in entries if entry.requirement.specifier
    )

    if len(urls) > 1:
        return MergeConflict(
            name=name,
            kind="conflicting-urls",
            message=(
                f"conflicting direct URLs for {name!r}: " + _format_lines(with_url)
            ),
            lines=with_url,
        )

    if urls and with_specifier:
        return MergeConflict(
            name=name,
            kind="url-with-specifier",
            message=(
                f"direct URL and version specifier both given for {name!r}: "
                + _format_lines(with_url + with_specifier)
            ),
            lines=with_url + with_specifier,
        )

    extras = tuple(
        sorted(
            {
                canonicalize_name(extra)
                for entry in requirements
                for extra in entry.requirement.extras
            }
        )
    )

    if urls:
        (url,) = urls
        return MergedRequirement(
            name=name,
            extras=extras,
            specifier=SpecifierSet(),
            url=url,
            lines=lines,
        )

    specifier = SpecifierSet()
    for entry in entries:
        specifier &= entry.requirement.specifier
    if specifier and specifier.to_range().is_empty:
        return MergeConflict(
            name=name,
            kind="unsatisfiable-specifier",
            message=(
                f"no version of {name!r} satisfies the combined specifier "
                f"{str(specifier)!r}: " + _format_lines(with_specifier)
            ),
            lines=with_specifier,
        )

    return MergedRequirement(
        name=name, extras=extras, specifier=specifier, url=None, lines=lines
    )


def merge_requirements(
    lines: Iterable[RequirementLine],
    environments: Iterable[TargetEnvironment],
) -> MergeReport:
    """Merge requirement lines against an explicit environment matrix.

    :param lines: The input requirement lines, each labelled with its source.
    :param environments: The finite set of target environments to resolve.
    :returns: A :class:``MergeReport`` with one
        :class:``EnvironmentPlan`` per group of equivalent
        environments, a per-environment evaluation of every input line, and
        any unparseable input lines.
    :raises ValueError: If two target environments share a name.
    :raises packaging.markers.UndefinedEnvironmentName: If a marker
        references a key missing from a target environment.
    """
    targets = list(environments)
    names = [target.name for target in targets]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate target environment names: {sorted(names)!r}")

    parsed: list[_ParsedLine] = []
    errors: list[InputError] = []
    for line in lines:
        try:
            requirement = Requirement(line.line)
        except InvalidRequirement as exc:
            errors.append(
                InputError(source=line.source, line=line.line, message=str(exc))
            )
            continue
        parsed.append(
            _ParsedLine(
                line=line,
                requirement=requirement,
                name=canonicalize_name(requirement.name),
            )
        )

    plans: list[EnvironmentPlan] = []
    plan_index: dict[
        tuple[tuple[MergedRequirement, ...], tuple[MergeConflict, ...]], int
    ] = {}
    evaluations: list[LineEvaluation] = []

    for target in targets:
        active: dict[NormalizedName, list[_ParsedLine]] = {}
        states: list[tuple[_ParsedLine, bool]] = []
        for entry in parsed:
            applies = _marker_applies(entry.requirement.marker, target)
            states.append((entry, applies))
            if applies:
                active.setdefault(entry.name, []).append(entry)

        applied: set[int] = set()
        requirements: list[MergedRequirement] = []
        conflicts: list[MergeConflict] = []
        for name in sorted(active):
            entries = active[name]
            if not any(not entry.line.constraint for entry in entries):
                # Constraints alone never activate a package.
                continue
            merged = _merge_group(name, entries)
            if isinstance(merged, MergedRequirement):
                requirements.append(merged)
                applied.update(id(entry.line) for entry in entries)
            else:
                conflicts.append(merged)

        plan_key = (tuple(requirements), tuple(conflicts))
        index = plan_index.get(plan_key)
        if index is None:
            plan_index[plan_key] = len(plans)
            plans.append(
                EnvironmentPlan(
                    environments=(target.name,),
                    requirements=plan_key[0],
                    conflicts=plan_key[1],
                )
            )
        else:
            previous = plans[index]
            plans[index] = dataclasses.replace(
                previous, environments=(*previous.environments, target.name)
            )

        evaluations.extend(
            LineEvaluation(
                environment=target.name,
                line=entry.line,
                active=applies,
                applied=applies and id(entry.line) in applied,
            )
            for entry, applies in states
        )

    return MergeReport(
        plans=tuple(plans),
        evaluations=tuple(evaluations),
        errors=tuple(errors),
    )
