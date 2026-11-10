"""Tests for the strict cross-environment entry point ``Pylock.select_for_target``.

The strict entry point must never inspect the build machine: every decision is
driven by the explicitly provided target environment and compatible tags.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, cast

import packaging.pylock as pylock_module
import pretend
import pytest
from packaging.markers import Marker
from packaging.pylock import (
    Package,
    PackageArchive,
    PackageDirectory,
    PackageSdist,
    PackageVcs,
    PackageWheel,
    Pylock,
    PylockSelectError,
)
from packaging.specifiers import SpecifierSet
from packaging.tags import Tag
from packaging.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from packaging.markers import Environment
    from packaging.utils import NormalizedName

_Artifact: TypeAlias = (
    PackageVcs | PackageDirectory | PackageArchive | PackageWheel | PackageSdist
)


_LOCK_PATH = Path(__file__).parent / "pylock" / "pylock.cross-platform.toml"


def _load_lock() -> Pylock:
    return Pylock.from_dict(tomllib.loads(_LOCK_PATH.read_text()))


def _environment(
    *,
    major: int,
    minor: int,
    micro: int,
    sys_platform: str,
    os_name: str,
    platform_system: str,
    machine: str,
) -> Environment:
    short = f"{major}.{minor}"
    full = f"{major}.{minor}.{micro}"
    return {
        "implementation_name": "cpython",
        "implementation_version": full,
        "os_name": os_name,
        "platform_machine": machine,
        "platform_release": "release",
        "platform_system": platform_system,
        "platform_version": "version",
        "python_full_version": full,
        "platform_python_implementation": "CPython",
        "python_version": short,
        "sys_platform": sys_platform,
    }


_linux_312 = _environment(
    major=3,
    minor=12,
    micro=7,
    sys_platform="linux",
    os_name="posix",
    platform_system="Linux",
    machine="x86_64",
)
_linux_311 = _environment(
    major=3,
    minor=11,
    micro=9,
    sys_platform="linux",
    os_name="posix",
    platform_system="Linux",
    machine="x86_64",
)
_windows_312 = _environment(
    major=3,
    minor=12,
    micro=7,
    sys_platform="win32",
    os_name="nt",
    platform_system="Windows",
    machine="AMD64",
)
_windows_311 = _environment(
    major=3,
    minor=11,
    micro=9,
    sys_platform="win32",
    os_name="nt",
    platform_system="Windows",
    machine="AMD64",
)

_linux_tags_312 = [
    Tag("cp312", "cp312", "manylinux_2_17_x86_64"),
    Tag("py3", "none", "any"),
]
_linux_tags_311 = [
    Tag("cp311", "cp311", "manylinux_2_17_x86_64"),
    Tag("py3", "none", "any"),
]
_windows_tags_312 = [
    Tag("cp312", "cp312", "win_amd64"),
    Tag("py3", "none", "win_amd64"),
    Tag("py3", "none", "any"),
]
_windows_tags_311 = [
    Tag("cp311", "cp311", "win_amd64"),
    Tag("py3", "none", "win_amd64"),
    Tag("py3", "none", "any"),
]


@pytest.fixture()
def lock() -> Pylock:
    return _load_lock()


def _selection(
    lock: Pylock,
    environment: Environment,
    tags: list[Tag],
    *,
    extras: Collection[str] | None = None,
    dependency_groups: Collection[str] | None = None,
) -> dict[str, _Artifact]:
    return {
        package.name: artifact
        for package, artifact in lock.select_for_target(
            environment=environment,
            tags=tags,
            extras=extras,
            dependency_groups=dependency_groups,
        )
    }


def _wheel_filename(artifact: _Artifact) -> str:
    assert isinstance(artifact, PackageWheel)
    return artifact.filename


def test_selects_four_targets_from_one_lock(
    lock: Pylock,
) -> None:
    linux_312 = _selection(lock, _linux_312, _linux_tags_312)
    linux_311 = _selection(lock, _linux_311, _linux_tags_311)
    windows_312 = _selection(lock, _windows_312, _windows_tags_312)
    windows_311 = _selection(lock, _windows_311, _windows_tags_311)

    # The unconditional pure-Python wheel is always selected.
    for selection in (linux_312, linux_311, windows_312, windows_311):
        assert isinstance(selection["tomli-w"], PackageWheel)
        assert selection["tomli-w"].filename == "tomli_w-1.2.0-py3-none-any.whl"

    # Each target gets exactly the matching platform/Python speedup wheel.
    assert _wheel_filename(linux_312["speedup"]) == (
        "speedup-4.2.1-cp312-cp312-manylinux_2_17_x86_64.whl"
    )
    assert _wheel_filename(linux_311["speedup"]) == (
        "speedup-4.2.1-cp311-cp311-manylinux_2_17_x86_64.whl"
    )
    assert (
        _wheel_filename(windows_312["speedup"])
        == "speedup-4.2.1-py3-none-win_amd64.whl"
    )
    assert (
        _wheel_filename(windows_311["speedup"])
        == "speedup-4.2.1-py3-none-win_amd64.whl"
    )

    # The default group is honored without any host lookup.
    assert set(linux_312) == {"tomli-w", "speedup", "speedup-tests"}
    assert set(windows_311) == {"tomli-w", "speedup", "speedup-tests"}


def test_same_target_is_independent_of_simulated_host(
    lock: Pylock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Replace every host introspection entry point with an exploding stub.
    monkeypatch.setattr(
        pylock_module,
        "default_environment",
        pretend.raiser(AssertionError("host environment must not be queried")),
    )
    monkeypatch.setattr(
        pylock_module,
        "sys_tags",
        pretend.raiser(AssertionError("host tags must not be queried")),
    )
    monkeypatch.setattr(
        pylock_module,
        "_pep440_python_full_version",
        lambda value: value,
    )

    selected_on_linux_host = _selection(lock, _linux_312, _linux_tags_312)

    # A different simulated host must not change the result at all.
    monkeypatch.setattr(
        pylock_module,
        "default_environment",
        lambda: cast("Environment", {"python_full_version": "9.9.9"}),
    )
    monkeypatch.setattr(
        pylock_module,
        "sys_tags",
        lambda: iter([Tag("cp99", "cp99", "host_platform")]),
    )

    selected_on_windows_host = _selection(lock, _linux_312, _linux_tags_312)

    assert selected_on_linux_host.keys() == selected_on_windows_host.keys()
    for name in selected_on_linux_host:
        assert selected_on_linux_host[name] == selected_on_windows_host[name]
    assert _wheel_filename(selected_on_linux_host["speedup"]) == (
        "speedup-4.2.1-cp312-cp312-manylinux_2_17_x86_64.whl"
    )


def test_marker_evaluation_never_reaches_marker_default_environment(
    lock: Pylock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import packaging.markers as markers_module

    monkeypatch.setattr(
        markers_module,
        "default_environment",
        pretend.raiser(
            AssertionError("Marker.evaluate host fallback must not be used")
        ),
    )
    monkeypatch.setattr(
        pylock_module,
        "default_environment",
        pretend.raiser(AssertionError("host environment must not be queried")),
    )
    monkeypatch.setattr(
        pylock_module,
        "sys_tags",
        pretend.raiser(AssertionError("host tags must not be queried")),
    )

    selection = _selection(lock, _linux_312, _linux_tags_312)
    assert "speedup" in selection


def test_missing_environment_field_names_field_and_expression(
    lock: Pylock,
) -> None:
    partial = cast(
        "Environment",
        {key: value for key, value in _linux_312.items() if key != "sys_platform"},
    )
    with pytest.raises(PylockSelectError) as exc_info:
        _selection(lock, partial, _linux_tags_312)
    message = str(exc_info.value)
    assert "'sys_platform'" in message
    assert 'sys_platform == "linux"' in message
    assert "environments" in message


def test_missing_python_full_version_for_requires_python() -> None:
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        requires_python=SpecifierSet(">=3.12"),
        packages=[],
    )
    pylock.validate()
    with pytest.raises(PylockSelectError) as exc_info:
        list(
            pylock.select_for_target(
                environment={"sys_platform": "linux"},
                tags=[],
            )
        )
    message = str(exc_info.value)
    assert "'python_full_version'" in message
    assert "requires-python" in message


def test_missing_field_for_package_marker() -> None:
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        packages=[
            Package(
                name=cast("NormalizedName", "foo"),
                version=Version("1.0"),
                marker=Marker('platform_machine == "x86_64"'),
                directory=PackageDirectory(path="./foo"),
            ),
        ],
    )
    pylock.validate()
    with pytest.raises(PylockSelectError) as exc_info:
        list(
            pylock.select_for_target(
                environment={"python_full_version": "3.12.7"},
                tags=[],
            )
        )
    message = str(exc_info.value)
    assert "'platform_machine'" in message
    assert 'platform_machine == "x86_64"' in message
    assert "packages[0].marker" in message


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda env: env.__setitem__("python_version", "3.11"),
            "python_version '3.11' does not match python_full_version '3.12.7'",
        ),
        (
            lambda env: env.__setitem__("platform_python_implementation", "PyPy"),
            "implementation_name 'cpython' implies "
            "platform_python_implementation 'CPython'",
        ),
        (
            lambda env: env.__setitem__("os_name", "nt"),
            "sys_platform 'linux' implies os_name 'posix'",
        ),
        (
            lambda env: env.__setitem__("platform_system", "Windows"),
            "sys_platform 'linux' implies platform_system 'Linux'",
        ),
        (
            lambda env: env.__setitem__("implementation_version", "3.11.9"),
            "implementation_version '3.11.9' does not share its major.minor",
        ),
    ],
)
def test_contradictory_target_descriptions(
    mutate: Callable[[dict[str, str]], None],
    expected: str,
) -> None:
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        packages=[],
    )
    pylock.validate()
    contradictory = cast("dict[str, str]", dict(_linux_312))
    mutate(contradictory)
    with pytest.raises(PylockSelectError, match=expected):
        list(pylock.select_for_target(environment=contradictory, tags=[]))


def test_invalid_python_full_version() -> None:
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        packages=[],
    )
    pylock.validate()
    bad = cast("dict[str, str]", dict(_linux_312))
    bad["python_full_version"] = "not-a-version"
    with pytest.raises(PylockSelectError, match="not a valid PEP 440 version"):
        list(pylock.select_for_target(environment=bad, tags=[]))


def test_duplicate_matching_packages_is_an_error_with_locations() -> None:
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        packages=[
            Package(
                name=cast("NormalizedName", "foo"),
                version=Version("1.0"),
                directory=PackageDirectory(path="./foo-1"),
            ),
            Package(
                name=cast("NormalizedName", "foo"),
                version=Version("2.0"),
                directory=PackageDirectory(path="./foo-2"),
            ),
        ],
    )
    pylock.validate()
    with pytest.raises(
        PylockSelectError,
        match=r"Multiple packages with the name 'foo' are selected "
        r"at packages\[1\] and packages\[0\]",
    ):
        list(
            pylock.select_for_target(
                environment=_linux_312,
                tags=_linux_tags_312,
            )
        )


def test_no_applicable_artifact_names_package_and_index() -> None:
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        packages=[
            Package(
                name=cast("NormalizedName", "foo"),
                version=Version("1.0"),
                wheels=[
                    PackageWheel(
                        name="foo-1.0-cp312-cp312-win_amd64.whl",
                        path="foo-1.0-cp312-cp312-win_amd64.whl",
                        hashes={"sha256": "abc"},
                    )
                ],
            ),
        ],
    )
    pylock.validate()
    with pytest.raises(
        PylockSelectError,
        match=r"No wheel found matching the provided tags for package 'foo' "
        r"at packages\[0\], and no sdist available",
    ):
        list(
            pylock.select_for_target(
                environment=_linux_312,
                tags=_linux_tags_312,
            )
        )


def test_empty_tags_falls_back_to_sdist() -> None:
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        packages=[
            Package(
                name=cast("NormalizedName", "speedup"),
                version=Version("4.2.1"),
                sdist=PackageSdist(
                    path="speedup-4.2.1.tar.gz",
                    hashes={"sha256": "abc"},
                ),
                wheels=[
                    PackageWheel(
                        name="speedup-4.2.1-py3-none-win_amd64.whl",
                        path="speedup-4.2.1-py3-none-win_amd64.whl",
                        hashes={"sha256": "abc"},
                    )
                ],
            ),
        ],
    )
    pylock.validate()
    selected = list(pylock.select_for_target(environment=_windows_312, tags=[]))
    assert len(selected) == 1
    assert isinstance(selected[0][1], PackageSdist)


def test_unknown_extra_is_rejected(lock: Pylock) -> None:
    with pytest.raises(PylockSelectError, match="Undeclared extras: absent"):
        _selection(lock, _linux_312, _linux_tags_312, extras=["absent"])


def test_extras_gate_selection(lock: Pylock) -> None:
    without_extra = _selection(lock, _linux_312, _linux_tags_312)
    assert "speedup-native" not in without_extra

    with_extra = _selection(lock, _linux_312, _linux_tags_312, extras=["NATIVE"])
    assert isinstance(with_extra["speedup-native"], PackageDirectory)


def test_explicit_empty_groups_selects_no_groups(lock: Pylock) -> None:
    default_selection = _selection(lock, _linux_312, _linux_tags_312)
    assert "speedup-tests" in default_selection

    empty_groups = _selection(lock, _linux_312, _linux_tags_312, dependency_groups=[])
    assert "speedup-tests" not in empty_groups
    # Explicitly requesting the declared group still works.
    named_groups = _selection(
        lock, _linux_312, _linux_tags_312, dependency_groups=["test"]
    )
    assert "speedup-tests" in named_groups


def test_unknown_group_is_rejected(lock: Pylock) -> None:
    with pytest.raises(PylockSelectError, match="Undeclared dependency groups: absent"):
        _selection(
            lock,
            _linux_312,
            _linux_tags_312,
            dependency_groups=["absent"],
        )


def test_environment_and_requires_python_use_same_target(lock: Pylock) -> None:
    # A 3.13 target violates the lock's requires-python, regardless of host.
    py313 = _environment(
        major=3,
        minor=13,
        micro=1,
        sys_platform="linux",
        os_name="posix",
        platform_system="Linux",
        machine="x86_64",
    )
    with pytest.raises(
        PylockSelectError,
        match="does not satisfy the Python version requirement",
    ):
        _selection(lock, py313, _linux_tags_312)


def test_environments_marker_must_match_target(lock: Pylock) -> None:
    macos = _environment(
        major=3,
        minor=12,
        micro=7,
        sys_platform="darwin",
        os_name="posix",
        platform_system="Darwin",
        machine="arm64",
    )
    with pytest.raises(
        PylockSelectError,
        match="does not satisfy any of the environments specified",
    ):
        _selection(lock, macos, [Tag("cp312", "cp312", "macosx_11_0_arm64")])


def test_environment_and_tags_are_required_keyword_arguments(
    lock: Pylock,
) -> None:
    with pytest.raises(TypeError):
        lock.select_for_target(tags=_linux_tags_312)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        lock.select_for_target(environment=_linux_312)  # type: ignore[call-arg]


def test_only_referenced_variables_are_required() -> None:
    pylock = Pylock(
        lock_version=Version("1.0"),
        created_by="some_tool",
        packages=[
            Package(
                name=cast("NormalizedName", "foo"),
                version=Version("1.0"),
                marker=Marker('python_version == "3.12"'),
                directory=PackageDirectory(path="./foo"),
            ),
        ],
    )
    pylock.validate()
    minimal_environment = {"python_version": "3.12"}
    selected = list(
        pylock.select_for_target(
            environment=minimal_environment,
            tags=[],
        )
    )
    assert selected[0][0].name == "foo"


def test_convenience_select_still_uses_host(
    lock: Pylock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The host convenience method keeps its defaulting behavior: patch the
    # host queries to a Linux 3.12 target and call without arguments.
    monkeypatch.setattr(pylock_module, "default_environment", lambda: dict(_linux_312))
    monkeypatch.setattr(pylock_module, "sys_tags", lambda: iter(_linux_tags_312))
    selected: dict[str, _Artifact] = {
        package.name: artifact
        for package, artifact in lock.select(
            dependency_groups=[],
        )
    }
    assert _wheel_filename(selected["speedup"]) == (
        "speedup-4.2.1-cp312-cp312-manylinux_2_17_x86_64.whl"
    )
