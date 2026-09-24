Lockfile Requirement Merging
============================

.. currentmodule:: packaging.lockmerge

A lockfile service often collects several requirement lines for the same
project: some only apply on one platform, some request extras, some pin
versions, and some are *constraints* that should only narrow down a package
another line already selects.  :func:`merge_requirements` merges such
lines against a finite, explicitly listed matrix of target environments and
reports, per environment, the normalized constraint that actually applies.

Requirements are grouped by their normalized project name, each line's
marker is evaluated per environment first, and the surviving lines have
their extras unioned and their version specifiers intersected.  Conflicts —
a direct URL combined with a version specifier, two different URLs, or an
unsatisfiable specifier intersection — are reported per environment and
point back to the original input lines and their source labels.

Constraint lines never activate a package on their own.  Environments that
resolve to exactly the same plan share one :class:`EnvironmentPlan`;
the report only ever names the explicit environments it was given and never
invents a marker expression claiming to cover unknown environments.

.. versionadded:: 26.4

Usage
-----

.. doctest::

    >>> from packaging.lockmerge import (
    ...     RequirementLine, TargetEnvironment, merge_requirements)

    >>> windows = TargetEnvironment(
    ...     name="windows",
    ...     values={"sys_platform": "win32", "python_version": "3.12"})
    >>> linux = TargetEnvironment(
    ...     name="linux",
    ...     values={"sys_platform": "linux", "python_version": "3.12"},
    ...     extras={"test"})

    >>> report = merge_requirements(
    ...     [
    ...         RequirementLine('Foo_Bar>=1.0; sys_platform == "win32"', "win.txt"),
    ...         RequirementLine("foo-bar[baz]<3", "base.txt"),
    ...         RequirementLine('foo-bar[test]; extra == "test"', "test.txt"),
    ...         RequirementLine("foo-bar>=0.5", "constraints.txt", constraint=True),
    ...     ],
    ...     [windows, linux],
    ... )
    >>> for plan in report.plans:
    ...     print(plan.environments)
    ...     for req in plan.requirements:
    ...         print(" ", str(req))
    ('windows',)
      foo-bar[baz]<3,>=0.5,>=1.0
    ('linux',)
      foo-bar[baz,test]<3,>=0.5

Reference
---------

.. autofunction:: merge_requirements

.. autoclass:: RequirementLine
    :members:

.. autoclass:: TargetEnvironment
    :members:

.. autoclass:: MergedRequirement
    :members:

.. autoclass:: MergeConflict
    :members:

.. autoclass:: LineEvaluation
    :members:

.. autoclass:: EnvironmentPlan
    :members:

.. autoclass:: MergeReport
    :members:

.. autoclass:: InputError
    :members:
