Wheel Validation
================

.. currentmodule:: packaging.wheel

Read-only validation of wheel archives, e.g. before uploading to a package
index. The archive is streamed and never extracted to disk, installed, or
imported. Downloading and installing wheels are out of scope.

.. versionadded:: 26.4

Usage
-----

.. code-block:: python

    from packaging.wheel import validate_wheel

    issues = validate_wheel("dist/demo-1.0-py3-none-any.whl")
    for issue in issues:
        print(issue.kind, issue.member, issue.message)

``validate_wheel`` accepts a path or a seekable binary file object and
returns a list of independent :class:`WheelIssue` objects; an empty list
means the wheel passed every check. The checks cover:

* the wheel filename (distribution, version, and tags);
* the presence of exactly one ``.dist-info`` directory, named after the
  distribution and version;
* the ``METADATA`` ``Name``/``Version`` fields against the filename;
* the ``WHEEL`` ``Tag`` fields against the filename tags;
* every ``RECORD`` entry: the path must exist in the archive, and the
  digest and size must match the member, with ``RECORD`` itself and its
  signatures (``RECORD.jws``/``RECORD.p7s``) recorded with empty fields;
* archive hygiene: duplicate ZIP members, member paths that collide after
  normalization, and directory traversal attempts.

Digests are computed while streaming, and the number of members and the
total uncompressed size are capped by ``max_members`` and
``max_unpacked_bytes`` as a guard against hostile archives.

Reference
---------

.. autoclass:: WheelIssue
    :members:

.. autofunction:: validate_wheel
