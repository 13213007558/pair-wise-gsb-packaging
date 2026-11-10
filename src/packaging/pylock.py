"""Read, validate, and select from pylock files.

The public data model classes are frozen, keyword-only dataclasses whose
attributes correspond to fields in the pylock file specification.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    TypeVar,
    cast,
)
from urllib.parse import unquote, urlparse

from .markers import (
    Environment,
    Marker,
    _evaluate_markers,
    _marker_environment_keys,
    _pep440_python_full_version,
    _repair_python_full_version,
    default_environment,
)
from .specifiers import SpecifierSet
from .tags import create_compatible_tags_selector, sys_tags
from .utils import (
    NormalizedName,
    canonicalize_name,
    is_normalized_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from .version import InvalidVersion, Version

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Collection, Iterator
    from pathlib import Path

    from typing_extensions import Self

    from .tags import Tag

_logger = logging.getLogger(__name__)

__all__ = [
    "Package",
    "PackageArchive",
    "PackageDirectory",
    "PackageSdist",
    "PackageVcs",
    "PackageWheel",
    "Pylock",
    "PylockSelectError",
    "PylockUnsupportedVersionError",
    "PylockValidationError",
    "is_valid_pylock_path",
]


def __dir__() -> list[str]:
    return __all__


_T = TypeVar("_T")
_T2 = TypeVar("_T2")


class _FromMappingProtocol(Protocol):  # pragma: no cover
    @classmethod
    def _from_dict(cls, d: Mapping[str, Any]) -> Self: ...


_FromMappingProtocolT = TypeVar("_FromMappingProtocolT", bound=_FromMappingProtocol)


_PYLOCK_FILE_NAME_RE = re.compile(r"^pylock\.([^.]+)\.toml$")


def is_valid_pylock_path(path: Path) -> bool:
    """Check if the given path is a valid pylock file path."""
    return path.name == "pylock.toml" or bool(_PYLOCK_FILE_NAME_RE.fullmatch(path.name))


def _toml_key(key: str) -> str:
    return key.replace("_", "-")


def _toml_value(key: str, value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, (Version, Marker, SpecifierSet)):
        return str(value)
    if isinstance(value, Sequence) and key == "environments":
        return [str(v) for v in value]
    return value


def _toml_dict_factory(data: list[tuple[str, Any]]) -> dict[str, Any]:
    return {
        _toml_key(key): _toml_value(key, value)
        for key, value in data
        if value is not None
    }


def _get(d: Mapping[str, Any], expected_type: type[_T], key: str) -> _T | None:
    """Get a value from the dictionary and verify it's the expected type."""
    if (value := d.get(key)) is None:
        return None
    if not isinstance(value, expected_type) or (
        # Special case: bool is a subclass of int, but TOML distinguishes the two
        expected_type is int and isinstance(value, bool)
    ):
        raise PylockValidationError(
            f"Unexpected type {type(value).__name__} "
            f"(expected {expected_type.__name__})",
            context=key,
        )
    return value


def _get_required(d: Mapping[str, Any], expected_type: type[_T], key: str) -> _T:
    """Get a required value from the dictionary and verify it's the expected type."""
    if (value := _get(d, expected_type, key)) is None:
        raise _PylockRequiredKeyError(key)
    return value


def _get_sequence(
    d: Mapping[str, Any], expected_item_type: type[_T], key: str
) -> Sequence[_T] | None:
    """Get a list value from the dictionary and verify it's the expected items type."""
    if (value := _get(d, Sequence, key)) is None:  # type: ignore[type-abstract]
        return None
    if isinstance(value, (str, bytes)):
        # special case: str and bytes are Sequences, but we want to reject it
        raise PylockValidationError(
            f"Unexpected type {type(value).__name__} (expected Sequence)",
            context=key,
        )
    for i, item in enumerate(value):
        if not isinstance(item, expected_item_type):
            raise PylockValidationError(
                f"Unexpected type {type(item).__name__} "
                f"(expected {expected_item_type.__name__})",
                context=f"{key}[{i}]",
            )
    return value


def _get_as(
    d: Mapping[str, Any],
    expected_type: type[_T],
    target_type: Callable[[_T], _T2],
    key: str,
) -> _T2 | None:
    """Get a value from the dictionary, verify it's the expected type,
    and convert to the target type.

    This assumes the target_type constructor accepts the value.
    """
    if (value := _get(d, expected_type, key)) is None:
        return None
    try:
        return target_type(value)
    except Exception as e:
        raise PylockValidationError(e, context=key) from e


def _get_required_as(
    d: Mapping[str, Any],
    expected_type: type[_T],
    target_type: Callable[[_T], _T2],
    key: str,
) -> _T2:
    """Get a required value from the dict, verify it's the expected type,
    and convert to the target type."""
    if (value := _get_as(d, expected_type, target_type, key)) is None:
        raise _PylockRequiredKeyError(key)
    return value


def _get_sequence_as(
    d: Mapping[str, Any],
    expected_item_type: type[_T],
    target_item_type: Callable[[_T], _T2],
    key: str,
) -> list[_T2] | None:
    """Get list value from dictionary and verify expected items type."""
    if (value := _get_sequence(d, expected_item_type, key)) is None:
        return None
    result = []
    try:
        for item in value:
            typed_item = target_item_type(item)
            result.append(typed_item)
    except Exception as e:
        raise PylockValidationError(e, context=f"{key}[{len(result)}]") from e
    return result


def _get_object(
    d: Mapping[str, Any], target_type: type[_FromMappingProtocolT], key: str
) -> _FromMappingProtocolT | None:
    """Get a dictionary value from the dictionary and convert it to a dataclass."""
    if (value := _get(d, Mapping, key)) is None:  # type: ignore[type-abstract]
        return None
    try:
        return target_type._from_dict(value)
    except Exception as e:
        raise PylockValidationError(e, context=key) from e


def _get_sequence_of_objects(
    d: Mapping[str, Any], target_item_type: type[_FromMappingProtocolT], key: str
) -> list[_FromMappingProtocolT] | None:
    """Get a list value from the dictionary and convert its items to a dataclass."""
    if (value := _get_sequence(d, Mapping, key)) is None:  # type: ignore[type-abstract]
        return None
    result: list[_FromMappingProtocolT] = []
    try:
        for item in value:
            typed_item = target_item_type._from_dict(item)
            result.append(typed_item)
    except Exception as e:
        raise PylockValidationError(e, context=f"{key}[{len(result)}]") from e
    return result


def _get_required_sequence_of_objects(
    d: Mapping[str, Any], target_item_type: type[_FromMappingProtocolT], key: str
) -> Sequence[_FromMappingProtocolT]:
    """Get a required list value from the dictionary and convert its items to a
    dataclass."""
    if (result := _get_sequence_of_objects(d, target_item_type, key)) is None:
        raise _PylockRequiredKeyError(key)
    return result


def _validate_normalized_name(name: str) -> NormalizedName:
    """Validate that a string is a NormalizedName."""
    if not is_normalized_name(name):
        raise PylockValidationError(f"Name {name!r} is not normalized")
    return NormalizedName(name)


def _validate_path_url(path: str | None, url: str | None) -> None:
    if not path and not url:
        raise PylockValidationError("path or url must be provided")


def _path_name(path: str | None) -> str | None:
    if not path:
        return None
    # If the path is relative it MAY use POSIX-style path separators explicitly
    # for portability
    return path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _url_name(url: str | None) -> str | None:
    if not url:
        return None
    url_path = urlparse(url).path
    # The last path component is percent-encoded, so decode it to the file name
    return unquote(url_path.rsplit("/", 1)[-1])


def _validate_hashes(hashes: Mapping[str, Any]) -> Mapping[str, Any]:
    if not hashes:
        raise PylockValidationError("At least one hash must be provided")
    if not all(isinstance(hash_val, str) for hash_val in hashes.values()):
        raise PylockValidationError("Hash values must be strings")
    return hashes


class PylockValidationError(Exception):
    """Raised when when input data is not spec-compliant."""

    context: str | None = None
    message: str

    def __init__(
        self,
        cause: str | Exception,
        *,
        context: str | None = None,
    ) -> None:
        if isinstance(cause, PylockValidationError):
            if cause.context:
                self.context = (
                    f"{context}.{cause.context}" if context else cause.context
                )
            else:
                self.context = context
            self.message = cause.message
        else:
            self.context = context
            self.message = str(cause)

    def __str__(self) -> str:
        if self.context:
            return f"{self.message} in {self.context!r}"
        return self.message


class _PylockRequiredKeyError(PylockValidationError):
    def __init__(self, key: str) -> None:
        super().__init__("Missing required value", context=key)


class PylockUnsupportedVersionError(PylockValidationError):
    """Raised when encountering an unsupported `lock_version`."""


class PylockSelectError(Exception):
    """Base exception for errors raised by :meth:`Pylock.select`.

    .. versionadded:: 26.1
    """


# Known mappings between the ``implementation_name`` and
# ``platform_python_implementation`` marker variables. An unknown pair is not
# rejected, since implementations packaging does not know about may exist.
_IMPLEMENTATION_NAMES: Mapping[str, str] = {
    "cpython": "CPython",
    "pypy": "PyPy",
    "jython": "Jython",
    "ironpython": "IronPython",
    "graalpy": "GraalPy",
    "rustpython": "RustPython",
}

# Coherent values for the operating-system related marker variables. When
# ``sys_platform`` is given, the other two values must match this mapping.
_PLATFORM_TRIPLES: Mapping[str, tuple[str, str]] = {
    "linux": ("posix", "Linux"),
    "darwin": ("posix", "Darwin"),
    "win32": ("nt", "Windows"),
    "cygwin": ("posix", "CYGWIN_NT"),
    "emscripten": ("posix", "Emscripten"),
    "wasi": ("posix", "wasi"),
    "aix": ("posix", "AIX"),
    "freebsd": ("posix", "FreeBSD"),
    "openbsd": ("posix", "OpenBSD"),
    "netbsd": ("posix", "NetBSD"),
    "dragonfly": ("posix", "DragonFly"),
    "haiku": ("posix", "Haiku"),
    "sunos5": ("posix", "SunOS"),
}


def _validate_target_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Validate an explicit target marker environment for cross-environment use.

    Only the values actually provided are checked; nothing is filled in from
    the host. Obvious internal contradictions between related marker variables
    raise :class:`PylockSelectError`.
    """
    target: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(value, str):  # pragma: no cover - typing guarantee
            raise TypeError(
                f"environment value for {key!r} must be a str, "
                f"not {type(value).__name__}"
            )
        target[key] = value

    full_version = target.get("python_full_version")
    short_version = target.get("python_version")
    if full_version is not None:
        try:
            parsed_full_version = Version(full_version)
        except InvalidVersion:
            raise PylockSelectError(
                f"python_full_version {full_version!r} in provided target "
                f"environment is not a valid PEP 440 version"
            ) from None
        if short_version is not None:
            derived_short_version = (
                f"{parsed_full_version.major}.{parsed_full_version.minor}"
            )
            if derived_short_version != short_version:
                raise PylockSelectError(
                    f"Contradictory target Python description: "
                    f"python_version {short_version!r} does not match "
                    f"python_full_version {full_version!r} "
                    f"(which implies python_version == {derived_short_version!r})"
                )

    implementation_name = target.get("implementation_name")
    platform_implementation = target.get("platform_python_implementation")
    if (
        implementation_name is not None
        and platform_implementation is not None
        and (expected_implementation := _IMPLEMENTATION_NAMES.get(implementation_name))
        is not None
        and platform_implementation != expected_implementation
    ):
        raise PylockSelectError(
            f"Contradictory target Python description: "
            f"implementation_name {implementation_name!r} implies "
            f"platform_python_implementation {expected_implementation!r}, "
            f"but {platform_implementation!r} was provided"
        )

    if (
        implementation_name == "cpython"
        and full_version is not None
        and (implementation_version := target.get("implementation_version")) is not None
    ):
        try:
            parsed_implementation_version = Version(implementation_version)
        except InvalidVersion:
            raise PylockSelectError(
                f"implementation_version {implementation_version!r} in provided "
                f"target environment is not a valid PEP 440 version"
            ) from None
        if (
            parsed_implementation_version.major,
            parsed_implementation_version.minor,
        ) != (Version(full_version).major, Version(full_version).minor):
            raise PylockSelectError(
                f"Contradictory target Python description: "
                f"implementation_version {implementation_version!r} does not "
                f"share its major.minor with python_full_version "
                f"{full_version!r} for a CPython target"
            )

    sys_platform = target.get("sys_platform")
    os_name = target.get("os_name")
    platform_system = target.get("platform_system")
    if (
        sys_platform is not None
        and (expected_platform := _PLATFORM_TRIPLES.get(sys_platform)) is not None
    ):
        expected_os_name, expected_platform_system = expected_platform
        if os_name is not None and os_name != expected_os_name:
            raise PylockSelectError(
                f"Contradictory target Python description: "
                f"sys_platform {sys_platform!r} implies os_name "
                f"{expected_os_name!r}, but {os_name!r} was provided"
            )
        if platform_system is not None and platform_system != expected_platform_system:
            raise PylockSelectError(
                f"Contradictory target Python description: "
                f"sys_platform {sys_platform!r} implies platform_system "
                f"{expected_platform_system!r}, but {platform_system!r} was provided"
            )
    elif os_name is not None and platform_system is not None:
        coherent_pairs = set(_PLATFORM_TRIPLES.values())
        if (os_name, platform_system) not in coherent_pairs:
            raise PylockSelectError(
                f"Contradictory target Python description: "
                f"os_name {os_name!r} and platform_system {platform_system!r} "
                f"do not describe a known operating system"
            )

    return target


def _strict_evaluate_marker(
    marker: Marker,
    environment: Mapping[str, Any],
    *,
    location: str,
) -> bool:
    """Evaluate a marker solely against the provided target environment.

    Unlike :meth:`Marker.evaluate`, this never consults
    :func:`default_environment`, so a variable referenced by the marker but
    absent from the target is an error naming the field and the expression
    rather than silently being read from the host.
    """
    missing = sorted(_marker_environment_keys(marker._markers) - environment.keys())
    if missing:
        raise PylockSelectError(
            f"Target environment is missing {', '.join(repr(key) for key in missing)} "
            f"required to evaluate marker {str(marker)!r} in {location!r}; "
            f"provide the value(s) explicitly instead of relying on the host "
            f"environment"
        )
    evaluation_env = dict(environment)
    if "python_full_version" in evaluation_env:
        evaluation_env = _repair_python_full_version(evaluation_env)
    return _evaluate_markers(marker._markers, evaluation_env)


@dataclass(frozen=True, kw_only=True, slots=True)
class PackageVcs:
    """A package installed from a version control system."""

    type: str
    url: str | None = None
    path: str | None = None
    requested_revision: str | None = None
    commit_id: str
    subdirectory: str | None = None

    @classmethod
    def _from_dict(cls, d: Mapping[str, Any]) -> Self:
        package_vcs = cls(
            type=_get_required(d, str, "type"),
            url=_get(d, str, "url"),
            path=_get(d, str, "path"),
            requested_revision=_get(d, str, "requested-revision"),
            commit_id=_get_required(d, str, "commit-id"),
            subdirectory=_get(d, str, "subdirectory"),
        )
        _validate_path_url(package_vcs.path, package_vcs.url)
        return package_vcs


@dataclass(frozen=True, kw_only=True, slots=True)
class PackageDirectory:
    """A package installed from a local directory."""

    path: str
    editable: bool | None = None
    subdirectory: str | None = None

    @classmethod
    def _from_dict(cls, d: Mapping[str, Any]) -> Self:
        return cls(
            path=_get_required(d, str, "path"),
            editable=_get(d, bool, "editable"),
            subdirectory=_get(d, str, "subdirectory"),
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class PackageArchive:
    """A package installed from an archive."""

    url: str | None = None
    path: str | None = None
    size: int | None = None
    upload_time: datetime | None = None
    hashes: Mapping[str, str]
    subdirectory: str | None = None

    @classmethod
    def _from_dict(cls, d: Mapping[str, Any]) -> Self:
        package_archive = cls(
            url=_get(d, str, "url"),
            path=_get(d, str, "path"),
            size=_get(d, int, "size"),
            upload_time=_get(d, datetime, "upload-time"),
            hashes=_get_required_as(d, Mapping, _validate_hashes, "hashes"),  # type: ignore[type-abstract]
            subdirectory=_get(d, str, "subdirectory"),
        )
        _validate_path_url(package_archive.path, package_archive.url)
        return package_archive


@dataclass(frozen=True, kw_only=True, slots=True)
class PackageSdist:
    """A source distribution for a package."""

    name: str | None = None
    upload_time: datetime | None = None
    url: str | None = None
    path: str | None = None
    size: int | None = None
    hashes: Mapping[str, str]

    @classmethod
    def _from_dict(cls, d: Mapping[str, Any]) -> Self:
        package_sdist = cls(
            name=_get(d, str, "name"),
            upload_time=_get(d, datetime, "upload-time"),
            url=_get(d, str, "url"),
            path=_get(d, str, "path"),
            size=_get(d, int, "size"),
            hashes=_get_required_as(d, Mapping, _validate_hashes, "hashes"),  # type: ignore[type-abstract]
        )
        _validate_path_url(package_sdist.path, package_sdist.url)
        return package_sdist

    @property
    def filename(self) -> str:
        """Get the filename of the sdist.

        .. versionadded:: 26.1
        """
        filename = self.name or _path_name(self.path) or _url_name(self.url)
        if not filename:
            raise PylockValidationError("Cannot determine sdist filename")
        return filename


@dataclass(frozen=True, kw_only=True, slots=True)
class PackageWheel:
    """A wheel distribution for a package."""

    name: str | None = None
    upload_time: datetime | None = None
    url: str | None = None
    path: str | None = None
    size: int | None = None
    hashes: Mapping[str, str]

    @classmethod
    def _from_dict(cls, d: Mapping[str, Any]) -> Self:
        package_wheel = cls(
            name=_get(d, str, "name"),
            upload_time=_get(d, datetime, "upload-time"),
            url=_get(d, str, "url"),
            path=_get(d, str, "path"),
            size=_get(d, int, "size"),
            hashes=_get_required_as(d, Mapping, _validate_hashes, "hashes"),  # type: ignore[type-abstract]
        )
        _validate_path_url(package_wheel.path, package_wheel.url)
        return package_wheel

    @property
    def filename(self) -> str:
        """Get the filename of the wheel."""
        filename = self.name or _path_name(self.path) or _url_name(self.url)
        if not filename:
            raise PylockValidationError("Cannot determine wheel filename")
        return filename


@dataclass(frozen=True, kw_only=True, slots=True)
class Package:
    """A package entry in a pylock file.

    A package has either distribution files or one direct source (VCS,
    directory, or archive).
    """

    name: NormalizedName
    version: Version | None = None
    marker: Marker | None = None
    requires_python: SpecifierSet | None = None
    dependencies: Sequence[Mapping[str, Any]] | None = None
    vcs: PackageVcs | None = None
    directory: PackageDirectory | None = None
    archive: PackageArchive | None = None
    index: str | None = None
    sdist: PackageSdist | None = None
    wheels: Sequence[PackageWheel] | None = None
    attestation_identities: Sequence[Mapping[str, Any]] | None = None
    tool: Mapping[str, Any] | None = None

    @classmethod
    def _from_dict(cls, d: Mapping[str, Any]) -> Self:
        package = cls(
            name=_get_required_as(d, str, _validate_normalized_name, "name"),
            version=_get_as(d, str, Version, "version"),
            requires_python=_get_as(d, str, SpecifierSet, "requires-python"),
            dependencies=_get_sequence(d, Mapping, "dependencies"),  # type: ignore[type-abstract]
            marker=_get_as(d, str, Marker, "marker"),
            vcs=_get_object(d, PackageVcs, "vcs"),
            directory=_get_object(d, PackageDirectory, "directory"),
            archive=_get_object(d, PackageArchive, "archive"),
            index=_get(d, str, "index"),
            sdist=_get_object(d, PackageSdist, "sdist"),
            wheels=_get_sequence_of_objects(d, PackageWheel, "wheels"),
            attestation_identities=_get_sequence(d, Mapping, "attestation-identities"),  # type: ignore[type-abstract]
            tool=_get(d, Mapping, "tool"),  # type: ignore[type-abstract]
        )
        distributions = bool(package.sdist) + len(package.wheels or [])
        direct_urls = (
            bool(package.vcs) + bool(package.directory) + bool(package.archive)
        )
        if distributions > 0 and direct_urls > 0:
            raise PylockValidationError(
                "None of vcs, directory, archive must be set if sdist or wheels are set"
            )
        if distributions == 0 and direct_urls != 1:
            raise PylockValidationError(
                "Exactly one of vcs, directory, archive must be set "
                "if sdist and wheels are not set"
            )
        for i, wheel in enumerate(package.wheels or []):
            try:
                (name, version, _, _) = parse_wheel_filename(wheel.filename)
            except Exception as e:
                raise PylockValidationError(
                    f"Invalid wheel filename {wheel.filename!r}",
                    context=f"wheels[{i}]",
                ) from e
            if name != package.name:
                raise PylockValidationError(
                    f"Name in {wheel.filename!r} is not consistent with "
                    f"package name {package.name!r}",
                    context=f"wheels[{i}]",
                )
            if package.version and version != package.version:
                raise PylockValidationError(
                    f"Version in {wheel.filename!r} is not consistent with "
                    f"package version {str(package.version)!r}",
                    context=f"wheels[{i}]",
                )
        if package.sdist:
            try:
                name, version = parse_sdist_filename(package.sdist.filename)
            except Exception as e:
                raise PylockValidationError(
                    f"Invalid sdist filename {package.sdist.filename!r}",
                    context="sdist",
                ) from e
            if name != package.name:
                raise PylockValidationError(
                    f"Name in {package.sdist.filename!r} is not consistent with "
                    f"package name {package.name!r}",
                    context="sdist",
                )
            if package.version and version != package.version:
                raise PylockValidationError(
                    f"Version in {package.sdist.filename!r} is not consistent with "
                    f"package version {str(package.version)!r}",
                    context="sdist",
                )
        try:
            for i, attestation_identity in enumerate(  # noqa: B007
                package.attestation_identities or []
            ):
                _get_required(attestation_identity, str, "kind")
        except Exception as e:
            raise PylockValidationError(
                e, context=f"attestation-identities[{i}]"
            ) from e
        return package

    @property
    def is_direct(self) -> bool:
        return not (self.sdist or self.wheels)


@dataclass(frozen=True, kw_only=True, slots=True)
class Pylock:
    """Represent a validated pylock file."""

    lock_version: Version
    environments: Sequence[Marker] | None = None
    requires_python: SpecifierSet | None = None
    extras: Sequence[NormalizedName] | None = None
    dependency_groups: Sequence[str] | None = None
    default_groups: Sequence[str] | None = None
    created_by: str
    packages: Sequence[Package]
    tool: Mapping[str, Any] | None = None

    @classmethod
    def _from_dict(cls, d: Mapping[str, Any]) -> Self:
        pylock = cls(
            lock_version=_get_required_as(d, str, Version, "lock-version"),
            environments=_get_sequence_as(d, str, Marker, "environments"),
            extras=_get_sequence_as(d, str, _validate_normalized_name, "extras"),
            dependency_groups=_get_sequence(d, str, "dependency-groups"),
            default_groups=_get_sequence(d, str, "default-groups"),
            created_by=_get_required(d, str, "created-by"),
            requires_python=_get_as(d, str, SpecifierSet, "requires-python"),
            packages=_get_required_sequence_of_objects(d, Package, "packages"),
            tool=_get(d, Mapping, "tool"),  # type: ignore[type-abstract]
        )
        if not Version("1") <= pylock.lock_version < Version("2"):
            raise PylockUnsupportedVersionError(
                f"pylock version {pylock.lock_version} is not supported"
            )
        if pylock.lock_version > Version("1.0"):
            _logger.warning(
                "pylock minor version %s is not supported", pylock.lock_version
            )
        return pylock

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], /) -> Self:
        """Create and validate a Pylock instance from a TOML dictionary.

        Raises :class:`PylockValidationError` if the input data is not
        spec-compliant.
        """
        return cls._from_dict(d)

    def to_dict(self) -> Mapping[str, Any]:
        """Convert the Pylock instance to a TOML dictionary."""
        return dataclasses.asdict(self, dict_factory=_toml_dict_factory)

    def validate(self) -> None:
        """Validate the Pylock instance against the specification.

        Raises :class:`PylockValidationError` otherwise."""
        self.from_dict(self.to_dict())

    def select(
        self,
        *,
        environment: Environment | None = None,
        tags: Sequence[Tag] | None = None,
        extras: Collection[str] | None = None,
        dependency_groups: Collection[str] | None = None,
        prefer_sdist_predicate: Callable[[NormalizedName], bool] | None = None,
    ) -> Iterator[
        tuple[
            Package,
            PackageVcs
            | PackageDirectory
            | PackageArchive
            | PackageWheel
            | PackageSdist,
        ]
    ]:
        """Select what to install from the lock file for the current machine.

        The *environment* and *tags* parameters represent the environment being
        selected for. If unspecified, ``packaging.markers.default_environment()`` and
        ``packaging.tags.sys_tags()`` are used.

        The *extras* parameter represents the extras to install.

        The *dependency_groups* parameter represents the groups to install. If
        unspecified, the default groups are used.

        The *prefer_sdist_predicate* parameter is called for packages with a source
        distribution. If it returns ``True``, the source distribution is selected
        before attempting wheel compatibility. If no source distribution is
        available, wheel selection proceeds as usual without calling the predicate.

        This is a convenience method for selecting for the running interpreter.
        When selecting for a different machine (for example, a release build that
        publishes artifacts for several target systems), use
        :meth:`select_for_target`, which never falls back to host-derived values.

        This method must be used on valid Pylock instances (i.e. one obtained
        from :meth:`Pylock.from_dict` or if constructed manually, after calling
        :meth:`Pylock.validate`).

        .. versionadded:: 26.1

        .. versionchanged:: 26.3
            Added the *prefer_sdist_predicate* parameter.

        .. versionchanged:: 26.4
            Raise :class:`PylockSelectError` if passed extras or dependency groups
            that are not declared in the corresponding pylock fields.
        """
        resolved_tags: Sequence[Tag] = list(sys_tags()) if tags is None else tags
        base_environment: dict[str, str] = dict(
            cast("Mapping[str, str]", environment or default_environment())
        )
        evaluation_environment = self._build_selection_environment(
            base_environment,
            extras,
            dependency_groups,
        )
        python_full_version = _pep440_python_full_version(
            base_environment["python_full_version"]
        )

        def evaluate_marker(
            marker: Marker,
            marker_environment: Mapping[str, Any],
            *,
            location: str,
        ) -> bool:
            return marker.evaluate(
                marker_environment,
                context="requirement" if location == "environments" else "lock_file",
            )

        yield from self._select_artifacts(
            tags=resolved_tags,
            base_environment=base_environment,
            evaluation_environment=evaluation_environment,
            python_full_version=python_full_version,
            prefer_sdist_predicate=prefer_sdist_predicate,
            evaluate_marker=evaluate_marker,
        )

    def select_for_target(
        self,
        *,
        environment: Environment | Mapping[str, str],
        tags: Sequence[Tag],
        extras: Collection[str] | None = None,
        dependency_groups: Collection[str] | None = None,
        prefer_sdist_predicate: Callable[[NormalizedName], bool] | None = None,
    ) -> Iterator[
        tuple[
            Package,
            PackageVcs
            | PackageDirectory
            | PackageArchive
            | PackageWheel
            | PackageSdist,
        ]
    ]:
        """Strictly select what to install for an explicitly described target.

        Unlike :meth:`select`, this entry point performs no host introspection
        at all: it never calls ``packaging.markers.default_environment()`` or
        ``packaging.tags.sys_tags()``. The target marker *environment* and the
        priority-ordered compatible *tags* are therefore both required keyword
        arguments, and the same description drives every decision: the
        lock-level ``environments`` and ``requires-python``, each package's
        ``marker`` and ``requires-python``, and wheel tag matching.

        Only the environment variables actually referenced by the evaluated
        markers need to be provided, but any referenced variable that is
        missing raises :class:`PylockSelectError` naming the field and the
        marker expression instead of being silently filled in from the build
        machine. The target description is also checked for obvious internal
        contradictions (such as a ``python_version`` that does not match
        ``python_full_version``).

        The set semantics for *extras* and *dependency_groups* follow the
        pylock convention: ``dependency_groups=None`` selects the lock file's
        default groups, while an explicit empty collection selects no groups;
        extras not declared in the lock file's ``extras`` field and groups not
        declared in ``dependency-groups`` or ``default-groups`` are errors.

        :raises PylockSelectError: if the target description is contradictory
            or incomplete, the lock file does not apply to the target, the
            same package name is selected more than once, no artifact matches
            the target, or an unknown extra or group is requested.

        .. versionadded:: 26.4
        """
        base_environment = _validate_target_environment(
            cast("Mapping[str, str]", environment)
        )
        evaluation_environment = self._build_selection_environment(
            base_environment,
            extras,
            dependency_groups,
        )

        def evaluate_marker(
            marker: Marker,
            marker_environment: Mapping[str, Any],
            *,
            location: str,
        ) -> bool:
            return _strict_evaluate_marker(
                marker,
                marker_environment,
                location=location,
            )

        yield from self._select_artifacts(
            tags=tags,
            base_environment=base_environment,
            evaluation_environment=evaluation_environment,
            python_full_version=None,
            prefer_sdist_predicate=prefer_sdist_predicate,
            evaluate_marker=evaluate_marker,
        )

    def _build_selection_environment(
        self,
        base_environment: Mapping[str, str],
        extras: Collection[str] | None,
        dependency_groups: Collection[str] | None,
    ) -> dict[str, str | frozenset[str]]:
        """Resolve extras and dependency groups into a marker environment.

        ``dependency_groups=None`` means the default groups apply; an explicit
        empty collection means no groups apply.
        """
        canonical_extras = {canonicalize_name(extra) for extra in extras or ()}
        unknown_extras = canonical_extras - set(self.extras or ())
        if unknown_extras:
            raise PylockSelectError(
                f"Undeclared extras: {', '.join(sorted(unknown_extras))}"
            )

        canonical_dependency_groups = {
            canonicalize_name(dependency_group)
            for dependency_group in dependency_groups or ()
        }
        unknown_dependency_groups = canonical_dependency_groups - {
            canonicalize_name(dependency_group)
            for dependency_group in [
                *(self.dependency_groups or ()),
                *(self.default_groups or ()),
            ]
        }
        if unknown_dependency_groups:
            raise PylockSelectError(
                f"Undeclared dependency groups: "
                f"{', '.join(sorted(unknown_dependency_groups))}"
            )

        # #. Gather the extras and dependency groups to install and set ``extras`` and
        #    ``dependency_groups`` for marker evaluation, respectively.
        #
        #    #. ``extras`` SHOULD be set to the empty set by default.
        #    #. ``dependency_groups`` SHOULD be the set created from
        #       :ref:`pylock-default-groups` by default.
        evaluation_environment = cast(
            "dict[str, str | frozenset[str]]",
            dict(
                base_environment,
                extras=frozenset(canonical_extras),
                dependency_groups=frozenset(
                    (self.default_groups or [])
                    if dependency_groups is None  # to allow selecting no group
                    else canonical_dependency_groups
                ),
            ),
        )
        return evaluation_environment

    def _resolve_python_full_version(
        self,
        base_environment: Mapping[str, str],
        python_full_version: str | None,
        *,
        specifier: SpecifierSet,
        location: str,
    ) -> str:
        """Get the target's ``python_full_version`` for a requires-python check.

        In strict mode the value is taken from the target description and its
        absence is an error naming the field rather than a host lookup.
        """
        if python_full_version is not None:
            return python_full_version
        if "python_full_version" not in base_environment:
            raise PylockSelectError(
                "Target environment is missing 'python_full_version' required to "
                f"evaluate requires-python {str(specifier)!r} in {location!r}; "
                "provide the value explicitly instead of relying on the host "
                "environment"
            )
        return _pep440_python_full_version(base_environment["python_full_version"])

    def _select_artifacts(
        self,
        *,
        tags: Sequence[Tag],
        base_environment: Mapping[str, str],
        evaluation_environment: Mapping[str, Any],
        python_full_version: str | None,
        prefer_sdist_predicate: Callable[[NormalizedName], bool] | None,
        evaluate_marker: Callable[..., bool],
    ) -> Iterator[
        tuple[
            Package,
            PackageVcs
            | PackageDirectory
            | PackageArchive
            | PackageWheel
            | PackageSdist,
        ]
    ]:
        compatible_tags_selector = create_compatible_tags_selector(tags)

        # #. Check if the metadata version specified by :ref:`pylock-lock-version` is
        #    supported; an error or warning MUST be raised as appropriate.
        # Covered by lock.validate() which is a precondition for this method.

        # #. If :ref:`pylock-requires-python` is specified, check that the environment
        #    being installed for meets the requirement; an error MUST be raised if it is
        #    not met.
        if self.requires_python:
            target_python_full_version = self._resolve_python_full_version(
                base_environment,
                python_full_version,
                specifier=self.requires_python,
                location="requires-python",
            )
            if not self.requires_python.contains(target_python_full_version):
                raise PylockSelectError(
                    f"python_full_version {target_python_full_version!r} "
                    f"in provided environment does not satisfy the Python version "
                    f"requirement {str(self.requires_python)!r}"
                )

        # #. If :ref:`pylock-environments` is specified, check that at least one of the
        #    environment marker expressions is satisfied; an error MUST be raised if no
        #    expression is satisfied.
        if self.environments is not None:
            for env_marker in self.environments:
                if evaluate_marker(
                    env_marker,
                    base_environment,
                    location="environments",
                ):
                    break
            else:
                raise PylockSelectError(
                    "Provided environment does not satisfy any of the "
                    "environments specified in the lock file"
                )

        # #. For each package listed in :ref:`pylock-packages`:
        selected_packages_by_name: dict[str, tuple[int, Package]] = {}
        for package_index, package in enumerate(self.packages):
            # #. If :ref:`pylock-packages-marker` is specified, check if it is
            #    satisfied;if it isn't, skip to the next package.
            if package.marker and not evaluate_marker(
                package.marker,
                evaluation_environment,
                location=f"packages[{package_index}].marker",
            ):
                continue

            # #. If :ref:`pylock-packages-requires-python` is specified, check if it is
            #    satisfied; an error MUST be raised if it isn't.
            if package.requires_python:
                target_python_full_version = self._resolve_python_full_version(
                    base_environment,
                    python_full_version,
                    specifier=package.requires_python,
                    location=f"packages[{package_index}].requires-python",
                )
                if not package.requires_python.contains(target_python_full_version):
                    raise PylockSelectError(
                        f"python_full_version {target_python_full_version!r} "
                        f"in provided environment does not satisfy the Python version "
                        f"requirement {str(package.requires_python)!r} for package "
                        f"{package.name!r} at packages[{package_index}]"
                    )

            # #. Check that no other conflicting instance of the package has been slated
            #    to be installed; an error about the ambiguity MUST be raised otherwise.
            if package.name in selected_packages_by_name:
                raise PylockSelectError(
                    f"Multiple packages with the name {package.name!r} are "
                    f"selected at packages[{package_index}] and "
                    f"packages[{selected_packages_by_name[package.name][0]}]"
                )

            # #. Check that the source of the package is specified appropriately (i.e.
            #    there are no conflicting sources in the package entry);
            #    an error MUST be raised if any issues are found.
            # Covered by lock.validate() which is a precondition for this method.

            # #. Add the package to the set of packages to install.
            selected_packages_by_name[package.name] = (package_index, package)

        # #. For each package to be installed:
        for package_index, package in selected_packages_by_name.values():
            # - If :ref:`pylock-packages-vcs` is set:
            if package.vcs is not None:
                yield package, package.vcs

            # - Else if :ref:`pylock-packages-directory` is set:
            elif package.directory is not None:
                yield package, package.directory

            # - Else if :ref:`pylock-packages-archive` is set:
            elif package.archive is not None:
                yield package, package.archive

            # - Else if source preference selects an available
            #   :ref:`pylock-packages-sdist`:
            elif (
                package.sdist is not None
                and prefer_sdist_predicate is not None
                and prefer_sdist_predicate(package.name)
            ):
                yield package, package.sdist

            # - Else if there are entries for :ref:`pylock-packages-wheels`:
            elif package.wheels:
                # #. Look for the appropriate wheel file based on
                #    :ref:`pylock-packages-wheels-name`; if one is not found then move
                #    on to :ref:`pylock-packages-sdist` or an error MUST be raised about
                #    a lack of source for the project.
                best_wheel = next(
                    compatible_tags_selector(
                        (wheel, parse_wheel_filename(wheel.filename)[-1])
                        for wheel in package.wheels
                    ),
                    None,
                )
                if best_wheel:
                    yield package, best_wheel
                elif package.sdist is not None:
                    yield package, package.sdist
                else:
                    raise PylockSelectError(
                        f"No wheel found matching the provided tags "
                        f"for package {package.name!r} "
                        f"at packages[{package_index}], "
                        f"and no sdist available as a fallback"
                    )

            # - Else if no :ref:`pylock-packages-wheels` file is found or
            #   :ref:`pylock-packages-sdist` is solely set:
            elif package.sdist is not None:
                yield package, package.sdist

            else:
                # Covered by lock.validate() which is a precondition for this method.
                raise NotImplementedError  # pragma: no cover
