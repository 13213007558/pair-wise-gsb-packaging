Lock Files
==========

.. currentmodule:: packaging.pylock

Parse and validate `pylock.toml files <https://packaging.python.org/en/latest/specifications/pylock-toml/>`_.

.. versionadded:: 26.0

Usage
-----

.. code-block:: python

    import tomllib
    from pathlib import Path

    from packaging.pylock import Package, PackageWheel, Pylock, is_valid_pylock_path
    from packaging.utils import NormalizedName
    from packaging.version import Version

    # validate a pylock file name
    assert is_valid_pylock_path(Path("pylock.example.toml"))

    # parse and validate pylock file
    toml_dict = tomllib.loads(Path("pylock.toml").read_text(encoding="utf-8"))
    pylock = Pylock.from_dict(toml_dict)
    # the resulting pylock object is validated against the specification,
    # else a PylockValidationError is raised

    # generate a pylock file
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        packages=[
            Package(
                name=NormalizedName("example-package"),
                version=Version("1.0.0"),
                wheels=[
                    PackageWheel(
                        url="https://example.com/example_package-1.0.0-py3-none-any.whl",
                        hashes={"sha256": "0fd.."},
                    )
                ],
            )
        ],
    )
    toml_dict = pylock.to_dict()
    # use a third-party library to serialize to TOML

    # you can validate a manually constructed Pylock class
    pylock.validate()

    # select packages to install for the current environment
    for package, artifact in pylock.select():
        print(f"Install {package.name} from {artifact}")

Selecting for another machine
------------------------------

:meth:`Pylock.select` defaults the marker environment and compatible tags to
the running interpreter, which is convenient for local installs. A release
build that publishes artifacts for several target systems must not let the
build machine leak into the target description. Use
:meth:`Pylock.select_for_target` instead: both the target marker environment
and the priority-ordered compatible tags are required keyword arguments, and
the method never calls :func:`packaging.markers.default_environment` or
:func:`packaging.tags.sys_tags`.

.. code-block:: python

    from packaging.tags import Tag

    target_environment = {
        "implementation_name": "cpython",
        "implementation_version": "3.12.7",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_release": "6.8.0",
        "platform_system": "Linux",
        "platform_version": "#1 SMP",
        "python_full_version": "3.12.7",
        "platform_python_implementation": "CPython",
        "python_version": "3.12",
        "sys_platform": "linux",
    }
    target_tags = [
        Tag("cp312", "cp312", "manylinux_2_17_x86_64"),
        Tag("py3", "none", "any"),
    ]

    for package, artifact in pylock.select_for_target(
        environment=target_environment,
        tags=target_tags,
    ):
        print(f"Install {package.name} from {artifact}")

Only the marker variables actually referenced by the lock file need to be
present. If a referenced variable is missing, :class:`PylockSelectError`
names the field and the marker expression rather than silently reading the
host value. The target description is also checked for obvious internal
contradictions, such as a ``python_version`` that disagrees with
``python_full_version``. The ``extras`` and ``dependency_groups`` arguments
follow the pylock convention: ``dependency_groups=None`` selects the lock
file's default groups, while an explicit empty collection selects no groups.

Validation
----------

:meth:`Pylock.from_dict` and :meth:`Pylock.validate` check the structure of
the lock file against the specification. Required fields must be present,
fields must have the expected types, package and extra names must be
normalized, and versions, specifiers and markers must parse. Each package
must have exactly one source: distribution files (``sdist`` or ``wheels``),
or one of ``vcs``, ``directory`` or ``archive``. Wheel and sdist filenames
must be parseable (see :func:`~packaging.utils.parse_wheel_filename` and
:func:`~packaging.utils.parse_sdist_filename`) and match the package name
and version.

Validation stops at the structure. URL and path fields, including
``packages.index``, are stored as-is. They are not checked to be valid URLs
or paths, and relative paths are not resolved against the location of the
lock file. A wheel or sdist entry with no ``name`` takes its filename from
the last component of its ``path`` or ``url``; nothing else is read from
these fields. ``hashes`` tables must be non-empty and their values must be
strings, but algorithm names and digest formats are not checked. Nothing is
downloaded, so checking hashes and sizes against the actual artifacts is up
to the caller.

Reference
---------

.. automodule:: packaging.pylock
    :members:
    :exclude-members: __init__, __new__
