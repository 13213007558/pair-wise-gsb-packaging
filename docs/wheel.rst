Wheels
======

.. currentmodule:: packaging.wheel

Read-only validation of wheel archives, e.g. before uploading to a
package index. The archive is only read, never extracted to disk, and
none of its code is imported. Downloading and installing are out of
scope.

Usage
-----

.. code-block:: python

    from packaging.wheel import validate_wheel

    issues = validate_wheel("demo-1.0-py3-none-any.whl")
    for issue in issues:
        print(issue)

    if not issues:
        print("wheel is consistent")

:func:`validate_wheel` accepts a path or a seekable binary file
object and returns an independent list of issues, each carrying the
archive member it relates to. It checks the filename, that there is
exactly one ``.dist-info`` directory, the
``Name``/``Version`` in
``METADATA``, the ``Tag`` entries in
``WHEEL``, and every path, digest, and size in
``RECORD`` (with the spec-mandated exceptions for
``RECORD`` itself and the
``RECORD.jws``/``RECORD.p7s`` signature
files). Duplicate ZIP members, path collisions after normalization, and
directory traversal are reported rather than masked by name-based ZIP
reads. Digests are computed while streaming, and the member count and
total decompressed size can be capped.

Reference
---------

.. autofunction:: validate_wheel

.. autoclass:: WheelIssue
    :members:
