Metadata Differences
====================

.. currentmodule:: packaging.metadata_diff

After a build, the core metadata in a source distribution's ``PKG-INFO`` and
in a wheel's ``METADATA`` can be compared without running any build code.
:func:`diff_metadata` parses the two already-produced email-header documents
with :class:`~packaging.metadata.Metadata` and returns a structured report of
how they differ.

The comparison is driven by the semantics of each field rather than by raw
text. For example, distribution names are normalized before comparison, PEP 440
version spellings that parse to the same version are treated as equal, and
``Requires-Dist`` entries are parsed into :class:`~packaging.requirements.Requirement`
objects so that equivalent spellings and entry ordering do not look like
dependency changes.

Dynamic fields
--------------

The core metadata ``Dynamic`` field only exists starting with
``Metadata-Version: 2.2``. The comparison therefore follows the promises made
by the *sdist's* metadata version:

* With Metadata 2.2 or newer, a field that is not listed in ``Dynamic`` must
  not be added, removed, or changed in the wheel. ``Name``, ``Version`` and
  ``Metadata-Version`` remain constrained regardless of any ``Dynamic`` entry.
* With older metadata versions there is no ``Dynamic`` mechanism, so the sdist
  makes no static-field promise. Such changes are reported as
  :attr:`DifferenceStatus.UNDETERMINED` with the evidence scope explained,
  rather than being called violations.

Each difference is classified into one of four statuses:

* :attr:`DifferenceStatus.FORMAT` -- the meaning is unchanged; only the
  serialization differs (normalization, equivalent version spelling, ordering).
* :attr:`DifferenceStatus.ALLOWED` -- the change is permitted, typically
  because the field was declared dynamic.
* :attr:`DifferenceStatus.VIOLATION` -- a field the sdist promised to keep
  static was added, removed, or changed.
* :attr:`DifferenceStatus.UNDETERMINED` -- the evidence is insufficient to
  decide. This includes malformed fields, unrecognized headers, and marker
  expressions whose logical equivalence cannot be proven.

The original field values from both documents are retained on every
:class:`FieldDifference` for manual review.

Usage
-----

.. doctest::

    >>> sdist = (
    ...     "Metadata-Version: 2.3\n"
    ...     "Name: demo\n"
    ...     "Version: 1.0\n"
    ...     "Requires-Dist: olddep==1.0\n"
    ... )
    >>> wheel = (
    ...     "Metadata-Version: 2.3\n"
    ...     "Name: demo\n"
    ...     "Version: 1.0\n"
    ...     "Requires-Dist: newdep==2.0\n"
    ... )
    >>> report = diff_metadata(sdist, wheel)
    >>> report.dynamic_contract_applies
    True
    >>> for difference in report.violations:
    ...     print(difference.field, difference.status.value)
    requires_dist violation
    >>> difference = report.violations[0]
    >>> difference.sdist_value
    ['olddep==1.0']
    >>> difference.wheel_value
    ['newdep==2.0']

Format-only changes are not violations:

.. doctest::

    >>> sdist = "Metadata-Version: 2.3\nName: Foo_Bar\nVersion: 1.0\n"
    >>> wheel = "Metadata-Version: 2.3\nName: foo-bar\nVersion: 1.0.0\n"
    >>> report = diff_metadata(sdist, wheel)
    >>> report.violations
    ()
    >>> [(difference.field, difference.status.value) for difference in report]
    [('name', 'format'), ('version', 'format')]

Reference
---------

.. autofunction:: diff_metadata

.. autoclass:: MetadataDifferenceReport
    :members:

.. autoclass:: FieldDifference
    :members:

.. autoclass:: DifferenceStatus
    :members:
