Merging Requirements
====================

.. currentmodule:: packaging.merge

.. versionadded:: 26.4

Lock-file producers often receive several
:class:`~packaging.requirements.Requirement` entries for the same project —
one guarded by a platform marker, one naming an extra, one pinning a
version — plus a separate list of constraints. :func:`merge_requirements`
evaluates every entry against an explicit, finite matrix of target
environments and merges the surviving entries into one normalized
:class:`MergedRequirement` per project and environment.

.. doctest::

    >>> from packaging.merge import (
    ...     SourcedRequirement, TargetEnvironment, merge_requirements,
    ... )
    >>> requirements = [
    ...     SourcedRequirement.parse("foo[bar]>=1.0; sys_platform == 'win32'", "win.txt:1"),
    ...     SourcedRequirement.parse("Foo<3", "base.txt:7"),
    ... ]
    >>> environments = [
    ...     TargetEnvironment("windows", {"sys_platform": "win32"}),
    ...     TargetEnvironment("linux", {"sys_platform": "linux"}),
    ... ]
    >>> report = merge_requirements(requirements, environments)
    >>> for group in report.groups:
    ...     print(group.environments, [str(r) for r in group.requirements])
    ('windows',) ['foo[bar]<3,>=1.0']
    ('linux',) ['foo<3']

The merge follows a few conservative rules:

* Requirements are grouped by canonicalized project name, so differently
  spelled entries (``Foo_Bar``, ``foo-bar``, ``foo.bar``) merge into one
  constraint. Extras are unioned and version specifiers intersected.
* Constraints passed via ``constraints=`` only tighten projects a
  requirement already activated in the same environment; a constraint
  alone never creates an install item.
* Equivalent environments share one :class:`EnvironmentGroup` labelled
  with each member environment. No marker expression is synthesized to
  generalize the group to environments outside the input matrix.
* Conflicts — a direct URL combined with a version specifier, two
  different direct URLs, or a version intersection no release can satisfy
  — are reported as :class:`MergeConflict` values whose
  :attr:`~MergeConflict.sources` point back at the responsible
  :class:`SourcedRequirement` inputs, source labels and raw lines
  included.

Reference
---------

.. autoclass:: packaging.merge.SourcedRequirement
    :members:

.. autoclass:: packaging.merge.TargetEnvironment
    :members:

.. autoclass:: packaging.merge.MergedRequirement
    :members:

.. autoclass:: packaging.merge.MergeConflict
    :members:

.. autoclass:: packaging.merge.ConflictKind
    :members:

.. autoclass:: packaging.merge.EnvironmentGroup
    :members:

.. autoclass:: packaging.merge.MergeReport
    :members:

.. autofunction:: packaging.merge.merge_requirements

