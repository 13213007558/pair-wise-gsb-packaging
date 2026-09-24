# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Merge requirements across a finite matrix of target environments.

Lock-file producers frequently receive several
:class:`~packaging.requirements.Requirement` entries for the same project:
one guarded by a platform marker, one naming an extra, one pinning a
version, and so on. This module evaluates every entry against an explicit,
finite list of target environments and merges the surviving entries into
one normalized constraint per project per environment.

The merge is intentionally conservative:

* Environments are never generalized. Equivalent environments are reported
  as a single :class:`EnvironmentGroup` carrying each member's label, but
  no marker expression is synthesized to stand in for environments that
  were not part of the input matrix.
* Constraints (pins supplied via ``constraints=``) only tighten projects
  that a requirement already activated in the same environment; a
  constraint alone never creates an install item.
* Conflicts — a direct URL combined with a version specifier, two
  different direct URLs, or a version intersection no release can satisfy
  — are reported as :class:`MergeConflict` values that reference the
  originating :class:`SourcedRequirement` inputs, source labels and raw
  lines included.

>>> reqs = [
...     SourcedRequirement.parse('foo[bar]>=1.0; sys_platform == "win32"', "win.txt:1"),
...     SourcedRequirement.parse("Foo<3", "base.txt:7"),
... ]
>>> envs = [
...     TargetEnvironment("windows", {"sys_platform": "win32"}),
...     TargetEnvironment("linux", {"sys_platform": "linux"}),
... ]
>>> report = merge_requirements(reqs, envs)
>>> for group in report.groups:
...     print(group.environments, [str(r) for r in group.requirements])
('windows',) ['foo[bar]<3,>=1.0']
('linux',) ['foo<3']
"""

from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .requirements import Requirement
from .specifiers import SpecifierSet
from .utils import NormalizedName, canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "ConflictKind",
    "EnvironmentGroup",
    "MergeConflict",
    "MergeReport",
    "MergedRequirement",
    "SourcedRequirement",
    "TargetEnvironment",
    "merge_requirements",
]


def __dir__() -> list[str]:
    return __all__


class ConflictKind(enum.Enum):
    """The kind of a :class:`MergeConflict`."""

    #: A direct URL requirement met a version specifier (from another
    #: requirement or from a constraint) for the same project.
    URL_VS_SPECIFIER = "url-vs-specifier"

    #: Two requirements named different direct URLs for the same project.
    CONFLICTING_URLS = "conflicting-urls"

    #: The merged version specifier admits no release.
    UNSATISFIABLE_SPECIFIERS = "unsatisfiable-specifiers"


@dataclass(frozen=True)
class SourcedRequirement:
    """A :class:`~packaging.requirements.Requirement` tied to its origin.

    :param requirement: The parsed requirement.
    :param source: A label identifying where the requirement came from,
        e.g. ``"requirements.txt:12"``. Preserved verbatim and echoed back
        on merged results and conflicts so applications can point users at
        the offending input.
    :param line: The raw input line the requirement was parsed from.
    """

    requirement: Requirement
    source: str
    line: str

    @classmethod
    def parse(cls, line: str, source: str) -> SourcedRequirement:
        """Build from a raw requirement ``line`` and a ``source`` label."""
        return cls(requirement=Requirement(line), source=source, line=line)


@dataclass(frozen=True)
class TargetEnvironment:
    """One member of the explicit target environment matrix.

    :param label: A human-facing name for the environment, echoed back on
        the resulting :class:`EnvironmentGroup`.
    :param environment: Marker environment overrides passed to
        :meth:`packaging.markers.Marker.evaluate`. Any key of
        :func:`packaging.markers.default_environment` may be overridden;
        omitted keys fall back to the current process. Include ``"extra"``
        to activate requirements guarded by an ``extra == ...`` marker.
    """

    label: str
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MergedRequirement:
    """The normalized constraint one environment group needs for a project.

    :param name: The canonicalized project name.
    :param specifier: The intersection of every activated version
        specifier, including applicable constraints.
    :param extras: The union of the extras named by activated requirements.
    :param url: The direct URL every activated requirement agreed on, if any.
    :param sources: The :class:`SourcedRequirement` inputs that contributed,
        requirements first then constraints, each in input order.
    """

    name: NormalizedName
    specifier: SpecifierSet
    extras: frozenset[str]
    url: str | None
    sources: tuple[SourcedRequirement, ...]

    def __str__(self) -> str:
        """Render as a canonical requirement string (without any marker).

        >>> str(MergedRequirement(
        ...     name=canonicalize_name("Foo"),
        ...     specifier=SpecifierSet(">=1.0,<3"),
        ...     extras=frozenset({"bar"}),
        ...     url=None,
        ...     sources=(),
        ... ))
        'foo[bar]<3,>=1.0'
        """
        extras = f"[{','.join(sorted(self.extras))}]" if self.extras else ""
        if self.url is not None:
            return f"{self.name}{extras} @ {self.url}"
        return f"{self.name}{extras}{self.specifier}"


@dataclass(frozen=True)
class MergeConflict:
    """A merge that failed, pointing back at the responsible inputs.

    :param kind: The :class:`ConflictKind`.
    :param name: The canonicalized project name the conflict is about.
    :param sources: The :class:`SourcedRequirement` inputs responsible for
        the conflict — only those carrying a URL or a non-empty specifier,
        so applications can highlight exactly the lines that clash.
    :param detail: A human-readable explanation.
    """

    kind: ConflictKind
    name: NormalizedName
    sources: tuple[SourcedRequirement, ...]
    detail: str


@dataclass(frozen=True)
class EnvironmentGroup:
    """The merged outcome shared by one or more equivalent environments.

    :param environments: The labels of every input environment that
        produced exactly this outcome. No marker expression is synthesized:
        membership is limited to the environments explicitly supplied in
        the input matrix.
    :param requirements: The successfully merged constraints, sorted by
        project name.
    :param conflicts: The :class:`MergeConflict` values raised while
        merging, sorted by kind then project name. A project named here
        does not appear in :attr:`requirements`.
    """

    environments: tuple[str, ...]
    requirements: tuple[MergedRequirement, ...]
    conflicts: tuple[MergeConflict, ...]


@dataclass(frozen=True)
class MergeReport:
    """The result of :func:`merge_requirements`.

    :param groups: One :class:`EnvironmentGroup` per distinct merged
        outcome, in first-seen order.
    """

    groups: tuple[EnvironmentGroup, ...]

    def for_environment(self, label: str) -> EnvironmentGroup:
        """Return the :class:`EnvironmentGroup` containing ``label``.

        :raises KeyError: If no input environment carried that label.
        """
        for group in self.groups:
            if label in group.environments:
                return group
        raise KeyError(label)


def _is_active(sourced: SourcedRequirement, environment: Mapping[str, str]) -> bool:
    marker = sourced.requirement.marker
    return marker is None or marker.evaluate(environment)


#: The merged outcome of one environment: its requirements and conflicts.
_Outcome = tuple[tuple[MergedRequirement, ...], tuple[MergeConflict, ...]]


def _merge_name(
    name: NormalizedName,
    requirements: list[SourcedRequirement],
    constraints: list[SourcedRequirement],
) -> MergedRequirement | MergeConflict:
    sources = (*requirements, *constraints)
    extras = frozenset(
        itertools.chain.from_iterable(r.requirement.extras for r in requirements)
    )
    urls = {r.requirement.url for r in requirements if r.requirement.url is not None}

    specifier = SpecifierSet()
    for sourced in sources:
        specifier = specifier & sourced.requirement.specifier

    url_sources = tuple(r for r in requirements if r.requirement.url is not None)
    spec_sources = tuple(s for s in sources if len(s.requirement.specifier))

    if len(urls) > 1:
        return MergeConflict(
            kind=ConflictKind.CONFLICTING_URLS,
            name=name,
            sources=url_sources,
            detail="conflicting direct URLs: " + ", ".join(sorted(urls)),
        )

    url = next(iter(urls), None)
    if url is not None and len(specifier):
        return MergeConflict(
            kind=ConflictKind.URL_VS_SPECIFIER,
            name=name,
            sources=(*url_sources, *spec_sources),
            detail=(
                f"direct URL {url} cannot be combined with "
                f"version specifier {specifier}"
            ),
        )

    if len(specifier) and specifier.is_unsatisfiable():
        return MergeConflict(
            kind=ConflictKind.UNSATISFIABLE_SPECIFIERS,
            name=name,
            sources=spec_sources,
            detail=f"merged specifier {specifier} admits no version",
        )

    return MergedRequirement(
        name=name,
        specifier=specifier,
        extras=extras,
        url=url,
        sources=sources,
    )


def _resolve_environment(
    environment: Mapping[str, str],
    requirements: tuple[SourcedRequirement, ...],
    constraints: tuple[SourcedRequirement, ...],
) -> _Outcome:
    active: dict[NormalizedName, list[SourcedRequirement]] = {}
    for sourced in requirements:
        if _is_active(sourced, environment):
            active.setdefault(canonicalize_name(sourced.requirement.name), []).append(
                sourced
            )

    # Constraints only tighten projects a requirement already activated in
    # this environment; alone they never create an install item.
    active_constraints: dict[NormalizedName, list[SourcedRequirement]] = {}
    for sourced in constraints:
        name = canonicalize_name(sourced.requirement.name)
        if name in active and _is_active(sourced, environment):
            active_constraints.setdefault(name, []).append(sourced)

    merged: list[MergedRequirement] = []
    conflicts: list[MergeConflict] = []
    for name in sorted(active):
        outcome = _merge_name(name, active[name], active_constraints.get(name, []))
        if isinstance(outcome, MergeConflict):
            conflicts.append(outcome)
        else:
            merged.append(outcome)

    conflicts.sort(key=lambda c: (c.kind.value, c.name))
    return tuple(merged), tuple(conflicts)


def merge_requirements(
    requirements: Iterable[SourcedRequirement],
    environments: Iterable[TargetEnvironment],
    constraints: Iterable[SourcedRequirement] = (),
) -> MergeReport:
    """Merge ``requirements`` per target environment.

    Every requirement's marker is evaluated against each
    :class:`TargetEnvironment`; surviving entries are grouped by
    canonicalized project name and merged into a single
    :class:`MergedRequirement` per name (extras unioned, version
    specifiers intersected, direct URLs unified). ``constraints`` are
    intersected too, but only for projects a requirement activated in the
    same environment.

    Environments whose merged outcome is identical share one
    :class:`EnvironmentGroup`; the group names its member environments
    explicitly and never generalizes to environments outside the input
    matrix.

    :param requirements: The requirement inputs, with source labels.
    :param environments: The explicit, finite target environment matrix.
        At least one environment is required.
    :param constraints: Optional constraint inputs (e.g. a pip constraints
        file), with source labels.
    :raises ValueError: If ``environments`` is empty or two environments
        share a label.
    """
    reqs = tuple(requirements)
    cons = tuple(constraints)
    envs = tuple(environments)

    if not envs:
        raise ValueError("at least one target environment is required")
    labels = [env.label for env in envs]
    if len(set(labels)) != len(labels):
        raise ValueError("target environment labels must be unique")

    groups: list[EnvironmentGroup] = []
    # Environments are equivalent when their merged requirements and
    # conflicts are identical; bucket by that outcome.
    index: dict[_Outcome, int] = {}
    for env in envs:
        outcome = _resolve_environment(env.environment, reqs, cons)
        slot = index.get(outcome)
        if slot is None:
            index[outcome] = len(groups)
            groups.append(
                EnvironmentGroup(
                    environments=(env.label,),
                    requirements=outcome[0],
                    conflicts=outcome[1],
                )
            )
        else:
            group = groups[slot]
            groups[slot] = EnvironmentGroup(
                environments=(*group.environments, env.label),
                requirements=group.requirements,
                conflicts=group.conflicts,
            )

    return MergeReport(groups=tuple(groups))
