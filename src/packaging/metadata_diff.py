# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Compare the core metadata of a source distribution and a built wheel.

The core metadata recorded in an sdist's ``PKG-INFO`` makes promises about the
distribution's identity and dependencies. The core metadata in a wheel's
``METADATA`` is expected to honor those promises, subject to the
``Dynamic`` field defined by the core metadata specification.

The functions in this module never execute a project's build code. They only
parse and compare two already-produced metadata documents, classifying each
difference according to the semantics of the field it concerns rather than
treating the documents as plain text.
"""

from __future__ import annotations

import dataclasses
import enum
import typing
from collections import Counter
from typing import TYPE_CHECKING, Any, Final

from . import requirements, utils
from .metadata import (
    _EMAIL_TO_RAW_MAPPING,
    _RAW_TO_EMAIL_MAPPING,
    _VALID_METADATA_VERSIONS,
    InvalidMetadata,
    Metadata,
    parse_email,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from .metadata import RawMetadata

__all__ = [
    "DifferenceStatus",
    "FieldDifference",
    "MetadataDifferenceReport",
    "diff_metadata",
]


def __dir__() -> list[str]:
    return __all__


#: First metadata version whose specification defines the ``Dynamic`` field,
#: i.e. the versions in which static core metadata makes an explicit contract
#: about which fields the build is allowed to change.
_DYNAMIC_CAPABLE_SINCE: Final = "2.2"

#: Raw field names that the core metadata specification forbids from dynamic.
_NEVER_DYNAMIC: Final = frozenset({"metadata_version", "name", "version"})

#: Repeated fields whose entry order carries no meaning.
_ORDER_INSENSITIVE_LIST_FIELDS: Final = frozenset(
    {
        "classifiers",
        "dynamic",
        "import_names",
        "import_namespaces",
        "license_files",
        "obsoletes",
        "obsoletes_dist",
        "platforms",
        "provides",
        "provides_dist",
        "provides_extra",
        "requires",
        "requires_external",
        "supported_platforms",
    }
)


class DifferenceStatus(str, enum.Enum):
    """How a difference between the two metadata documents is classified."""

    #: The values mean the same thing; only their serialization changed
    #: (e.g. name normalization, an equivalent version spelling or reordering).
    FORMAT = "format"
    #: The field was declared dynamic, or the change is otherwise permitted
    #: (such as a newer ``Metadata-Version`` emitted by the build backend).
    ALLOWED = "allowed"
    #: A field the sdist promised to keep static was added, removed, or changed.
    VIOLATION = "violation"
    #: There is not enough evidence to decide, or deciding would require
    #: proving something this module deliberately does not attempt, such as
    #: the logical equivalence of arbitrary marker expressions.
    UNDETERMINED = "undetermined"


@dataclasses.dataclass(frozen=True)
class FieldDifference:
    """A single difference found for one core metadata field.

    The original, unmodified field values from both documents are retained so
    that a human reviewer can inspect the evidence directly.
    """

    #: The core metadata field name in the underscore form used by
    #: :class:`~packaging.metadata.RawMetadata` (e.g. ``requires_dist``).
    field: str
    status: DifferenceStatus
    #: Human-readable explanation of the evidence and the classification.
    detail: str
    #: The raw value found in the sdist metadata, or ``None`` if absent there.
    sdist_value: Any
    #: The raw value found in the wheel metadata, or ``None`` if absent there.
    wheel_value: Any


@dataclasses.dataclass(frozen=True)
class MetadataDifferenceReport:
    """The structured result of comparing two core metadata documents.

    Iterating the report yields its :class:`FieldDifference` entries in stable
    field-name order.
    """

    sdist: Metadata
    wheel: Metadata
    differences: tuple[FieldDifference, ...]
    #: Whether the sdist's metadata version defines the ``Dynamic`` contract.
    #: When false, changes are reported as undetermined rather than violations
    #: because the sdist made no promise that static fields stay unchanged.
    dynamic_contract_applies: bool

    @property
    def violations(self) -> tuple[FieldDifference, ...]:
        """The differences classified as :attr:`DifferenceStatus.VIOLATION`."""
        return tuple(
            difference
            for difference in self.differences
            if difference.status is DifferenceStatus.VIOLATION
        )

    def __iter__(self) -> Iterator[FieldDifference]:
        return iter(self.differences)

    def __len__(self) -> int:
        return len(self.differences)


def _metadata_version_index(metadata: Metadata) -> int | None:
    """Return the ordinal of a parsed metadata version, or ``None`` if invalid."""
    try:
        value = metadata.metadata_version
    except InvalidMetadata:
        return None
    return _VALID_METADATA_VERSIONS.index(value)


def _dynamic_fields(metadata: Metadata) -> frozenset[str] | None:
    """Return the declared dynamic raw field names, or ``None`` if unparsable."""
    try:
        dynamic = metadata.dynamic
    except InvalidMetadata:
        return None
    if dynamic is None:
        return frozenset()
    # The validator lowercases the values but keeps their hyphenated header
    # spelling; translate them to the underscore form used as RawMetadata keys.
    return frozenset(_email_to_raw(email_name) for email_name in dynamic)


def _supports_dynamic(metadata: Metadata) -> bool:
    """Whether the document's metadata version defines the Dynamic contract."""
    index = _metadata_version_index(metadata)
    if index is None:
        return False
    return index >= _VALID_METADATA_VERSIONS.index(_DYNAMIC_CAPABLE_SINCE)


def _is_known(raw_key: str) -> bool:
    return raw_key in _RAW_TO_EMAIL_MAPPING


def _header_for(raw_key: str) -> str:
    """Map a RawMetadata key back to its email header name."""
    return _RAW_TO_EMAIL_MAPPING.get(raw_key, raw_key)


def _email_to_raw(email_name: str) -> str:
    """Map an email header name to a RawMetadata key when one is known."""
    return _EMAIL_TO_RAW_MAPPING.get(email_name, email_name)


def _field_status(
    raw_key: str,
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
) -> DifferenceStatus:
    """Classify a change for a field present on both sides.

    ``dynamic`` is ``None`` when the sdist's Dynamic field itself could not be
    parsed, which removes the evidence needed to assert a contract violation.
    Callers handling a field that exists on only one side must use
    :func:`_presence_status` instead, since a wheel-only field cannot claim the
    protection of a Dynamic declaration it was never part of.
    """
    if not contract_applies or dynamic is None:
        # Before Metadata 2.2 there is no Dynamic mechanism, and an unparsable
        # Dynamic field likewise gives no evidence. Missing evidence is reported
        # as such rather than treated as a violation.
        return DifferenceStatus.UNDETERMINED
    if raw_key in _NEVER_DYNAMIC:
        return DifferenceStatus.VIOLATION
    if raw_key in dynamic:
        return DifferenceStatus.ALLOWED
    return DifferenceStatus.VIOLATION


def _presence_status(
    raw_key: str,
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
    sdist_value_present: bool,
) -> DifferenceStatus:
    """Classify a field that exists on only one side of the comparison."""
    if not contract_applies or dynamic is None:
        return DifferenceStatus.UNDETERMINED
    if raw_key in _NEVER_DYNAMIC:
        return DifferenceStatus.VIOLATION
    # A Dynamic declaration only protects a field that the sdist itself listed;
    # a brand-new wheel field was never part of that declaration.
    if raw_key in dynamic and sdist_value_present:
        return DifferenceStatus.ALLOWED
    return DifferenceStatus.VIOLATION


def _valid_field(metadata: Metadata, raw_key: str) -> bool:
    """Whether the field parses cleanly through the Metadata validators."""
    try:
        getattr(metadata, raw_key)
    except InvalidMetadata:
        return False
    return True


def _difference(
    raw_key: str,
    status: DifferenceStatus,
    detail: str,
    sdist_value: Any,  # noqa: ANN401
    wheel_value: Any,  # noqa: ANN401
) -> list[FieldDifference]:
    return [FieldDifference(raw_key, status, detail, sdist_value, wheel_value)]


def _diff_scalar(
    raw_key: str,
    sdist: Metadata,
    wheel: Metadata,
    sdist_raw: RawMetadata,
    wheel_raw: RawMetadata,
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
) -> list[FieldDifference]:
    """Compare a single-valued field using the field's own semantics."""
    sdist_value = sdist_raw.get(raw_key)
    wheel_value = wheel_raw.get(raw_key)
    if sdist_value == wheel_value:
        return []
    header = _header_for(raw_key)

    if raw_key == "name":
        return _diff_name(raw_key, header, sdist, wheel, sdist_value, wheel_value)
    if raw_key == "version":
        return _diff_version(raw_key, header, sdist, wheel, sdist_value, wheel_value)
    if raw_key == "requires_python":
        return _diff_requires_python(
            raw_key,
            header,
            sdist,
            wheel,
            sdist_value,
            wheel_value,
            dynamic=dynamic,
            contract_applies=contract_applies,
        )
    if raw_key == "license_expression":
        return _diff_license_expression(
            raw_key,
            header,
            sdist,
            wheel,
            sdist_value,
            wheel_value,
            dynamic=dynamic,
            contract_applies=contract_applies,
        )

    status = _field_status(raw_key, dynamic=dynamic, contract_applies=contract_applies)
    if status is DifferenceStatus.ALLOWED:
        reason = "the field is declared dynamic"
    elif status is DifferenceStatus.VIOLATION:
        reason = "the field is static and was not declared dynamic"
    else:
        reason = "the sdist metadata predates Dynamic and makes no static-field promise"
    return _difference(
        raw_key,
        status,
        f"{header!r} changed and {reason}",
        sdist_value,
        wheel_value,
    )


def _diff_name(
    raw_key: str,
    header: str,
    sdist: Metadata,
    wheel: Metadata,
    sdist_value: Any,  # noqa: ANN401
    wheel_value: Any,  # noqa: ANN401
) -> list[FieldDifference]:
    sdist_name = utils.canonicalize_name(sdist.name)
    wheel_name = utils.canonicalize_name(wheel.name)
    if sdist_name == wheel_name:
        return _difference(
            raw_key,
            DifferenceStatus.FORMAT,
            f"{header!r} differs only in spelling; both normalize to "
            f"{sdist_name!r}",
            sdist_value,
            wheel_value,
        )
    return _difference(
        raw_key,
        DifferenceStatus.VIOLATION,
        f"{header!r} identifies a different project: {sdist_name!r} vs "
        f"{wheel_name!r}; Name cannot be declared dynamic",
        sdist_value,
        wheel_value,
    )


def _diff_version(
    raw_key: str,
    header: str,
    sdist: Metadata,
    wheel: Metadata,
    sdist_value: Any,  # noqa: ANN401
    wheel_value: Any,  # noqa: ANN401
) -> list[FieldDifference]:
    if sdist.version == wheel.version:
        return _difference(
            raw_key,
            DifferenceStatus.FORMAT,
            f"{header!r} differs only in spelling; both parse as {sdist.version!s}",
            sdist_value,
            wheel_value,
        )
    return _difference(
        raw_key,
        DifferenceStatus.VIOLATION,
        f"{header!r} changed from {sdist.version!s} to {wheel.version!s}; "
        "Version cannot be declared dynamic",
        sdist_value,
        wheel_value,
    )


def _diff_requires_python(
    raw_key: str,
    header: str,
    sdist: Metadata,
    wheel: Metadata,
    sdist_value: Any,  # noqa: ANN401
    wheel_value: Any,  # noqa: ANN401
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
) -> list[FieldDifference]:
    if sdist.requires_python == wheel.requires_python:
        return _difference(
            raw_key,
            DifferenceStatus.FORMAT,
            f"{header!r} differs only in formatting; both express "
            f"{sdist.requires_python!s}",
            sdist_value,
            wheel_value,
        )
    status = _field_status(raw_key, dynamic=dynamic, contract_applies=contract_applies)
    return _difference(
        raw_key,
        status,
        f"{header!r} changed the supported Python range from "
        f"{sdist.requires_python!s} to {wheel.requires_python!s}",
        sdist_value,
        wheel_value,
    )


def _diff_license_expression(
    raw_key: str,
    header: str,
    sdist: Metadata,
    wheel: Metadata,
    sdist_value: Any,  # noqa: ANN401
    wheel_value: Any,  # noqa: ANN401
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
) -> list[FieldDifference]:
    if sdist.license_expression == wheel.license_expression:
        return _difference(
            raw_key,
            DifferenceStatus.FORMAT,
            f"{header!r} differs only in spelling; both canonicalize to "
            f"{sdist.license_expression!s}",
            sdist_value,
            wheel_value,
        )
    status = _field_status(raw_key, dynamic=dynamic, contract_applies=contract_applies)
    return _difference(
        raw_key,
        status,
        f"{header!r} changed from {sdist.license_expression!s} to "
        f"{wheel.license_expression!s}",
        sdist_value,
        wheel_value,
    )


def _diff_order_insensitive_list(
    raw_key: str,
    sdist_value: list[str],
    wheel_value: list[str],
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
    normalize: Callable[[str], str] | None = None,
) -> list[FieldDifference]:
    """Compare a repeated field whose entry order is not semantically relevant."""
    header = _header_for(raw_key)
    if sdist_value == wheel_value:
        return []

    sdist_entries = (
        [normalize(item) for item in sdist_value] if normalize else list(sdist_value)
    )
    wheel_entries = (
        [normalize(item) for item in wheel_value] if normalize else list(wheel_value)
    )
    if Counter(sdist_entries) == Counter(wheel_entries):
        return _difference(
            raw_key,
            DifferenceStatus.FORMAT,
            f"{header!r} entries were reordered or respelled without a semantic "
            "change",
            sdist_value,
            wheel_value,
        )

    status = _field_status(raw_key, dynamic=dynamic, contract_applies=contract_applies)
    removed = Counter(sdist_entries) - Counter(wheel_entries)
    added = Counter(wheel_entries) - Counter(sdist_entries)
    details = []
    if removed:
        details.append(f"removed {sorted(removed.elements())}")
    if added:
        details.append(f"added {sorted(added.elements())}")
    return _difference(
        raw_key,
        status,
        f"{header!r} changed: {'; '.join(details)}",
        sdist_value,
        wheel_value,
    )


def _requirement_identity(
    requirement: requirements.Requirement,
) -> tuple[Any, ...]:
    """The parts that identify which dependency a requirement describes."""
    return (
        utils.canonicalize_name(requirement.name),
        frozenset(utils.canonicalize_name(extra) for extra in requirement.extras),
        requirement.url,
    )


def _group_requirements(
    requirements_list: list[requirements.Requirement],
) -> dict[tuple[Any, ...], list[requirements.Requirement]]:
    grouped: dict[tuple[Any, ...], list[requirements.Requirement]] = {}
    for requirement in requirements_list:
        grouped.setdefault(_requirement_identity(requirement), []).append(requirement)
    return grouped


def _diff_requires_dist(
    sdist: Metadata,
    wheel: Metadata,
    sdist_raw: RawMetadata,
    wheel_raw: RawMetadata,
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
) -> list[FieldDifference]:
    """Compare Requires-Dist with requirement-level, order-insensitive semantics."""
    raw_key = "requires_dist"
    sdist_raw_value = typing.cast("list[str] | None", sdist_raw.get(raw_key))
    wheel_raw_value = typing.cast("list[str] | None", wheel_raw.get(raw_key))
    sdist_value: list[str] = list(sdist_raw_value) if sdist_raw_value else []
    wheel_value: list[str] = list(wheel_raw_value) if wheel_raw_value else []
    if sdist_value == wheel_value:
        return []

    # Callers (_diff_known_field) have already validated both fields.
    sdist_requirements = list(sdist.requires_dist or [])
    wheel_requirements = list(wheel.requires_dist or [])
    sdist_groups = _group_requirements(sdist_requirements)
    wheel_groups = _group_requirements(wheel_requirements)
    sdist_identities = Counter(
        _requirement_identity(requirement) for requirement in sdist_requirements
    )
    wheel_identities = Counter(
        _requirement_identity(requirement) for requirement in wheel_requirements
    )

    differences: list[FieldDifference] = []
    format_pairs: list[tuple[str, str]] = []
    marker_pairs: list[tuple[str, str]] = []
    changed_pairs: list[tuple[str, str]] = []
    for identity in sorted(sdist_identities.keys() & wheel_identities.keys()):
        for source, target in zip(sdist_groups[identity], wheel_groups[identity]):
            if source == target:
                if str(source) != str(target):
                    format_pairs.append((str(source), str(target)))
            elif source.specifier == target.specifier:
                # Same name, extras, URL and version range: only the marker
                # differs. Arbitrary marker equivalence is not provable here,
                # so this is evidence to review rather than a proven violation.
                marker_pairs.append((str(source), str(target)))
            else:
                changed_pairs.append((str(source), str(target)))

    # Pure reordering of identical requirement strings.
    if (
        not format_pairs
        and not marker_pairs
        and not changed_pairs
        and Counter(map(str, sdist_requirements))
        == Counter(map(str, wheel_requirements))
    ):
        return _difference(
            raw_key,
            DifferenceStatus.FORMAT,
            "Requires-Dist entries were reordered without a value change",
            sdist_value,
            wheel_value,
        )

    if format_pairs:
        differences.append(
            FieldDifference(
                raw_key,
                DifferenceStatus.FORMAT,
                "requirements with identical meaning but different spelling: "
                + "; ".join(
                    f"{source!r} -> {target!r}" for source, target in format_pairs
                ),
                sdist_value,
                wheel_value,
            )
        )
    if marker_pairs:
        differences.append(
            FieldDifference(
                raw_key,
                DifferenceStatus.UNDETERMINED,
                "requirements differ only in marker expressions, whose logical "
                "equivalence is not provable here: "
                + "; ".join(
                    f"{source!r} -> {target!r}" for source, target in marker_pairs
                ),
                sdist_value,
                wheel_value,
            )
        )
    if changed_pairs:
        differences.append(
            FieldDifference(
                raw_key,
                _field_status(
                    raw_key, dynamic=dynamic, contract_applies=contract_applies
                ),
                "requirements changed their version constraint: "
                + "; ".join(
                    f"{source!r} -> {target!r}" for source, target in changed_pairs
                ),
                sdist_value,
                wheel_value,
            )
        )

    removed_identities = sdist_identities - wheel_identities
    added_identities = wheel_identities - sdist_identities
    if removed_identities or added_identities:
        details = []
        if removed_identities:
            details.append(
                "removed "
                + ", ".join(sorted(str(identity[0]) for identity in removed_identities))
            )
        if added_identities:
            details.append(
                "added "
                + ", ".join(sorted(str(identity[0]) for identity in added_identities))
            )
        differences.append(
            FieldDifference(
                raw_key,
                _field_status(
                    raw_key, dynamic=dynamic, contract_applies=contract_applies
                ),
                "Requires-Dist dependencies were added or removed: "
                + "; ".join(details),
                sdist_value,
                wheel_value,
            )
        )

    return differences


def _diff_project_urls(
    raw_key: str,
    sdist_value: dict[str, str],
    wheel_value: dict[str, str],
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
) -> list[FieldDifference]:
    header = _header_for(raw_key)
    if sdist_value == wheel_value:
        return []
    changed = sorted(
        label
        for label in frozenset(sdist_value) | frozenset(wheel_value)
        if sdist_value.get(label) != wheel_value.get(label)
    )
    return _difference(
        raw_key,
        _field_status(raw_key, dynamic=dynamic, contract_applies=contract_applies),
        f"{header!r} changed for labels {changed}",
        sdist_value,
        wheel_value,
    )


def _diff_metadata_version(
    sdist: Metadata,
    wheel: Metadata,
    sdist_raw: RawMetadata,
    wheel_raw: RawMetadata,
) -> list[FieldDifference]:
    raw_key = "metadata_version"
    header = _header_for(raw_key)
    sdist_value = sdist_raw.get(raw_key)
    wheel_value = wheel_raw.get(raw_key)
    if sdist_value == wheel_value:
        return []

    sdist_index = _metadata_version_index(sdist)
    wheel_index = _metadata_version_index(wheel)
    if sdist_index is None or wheel_index is None:
        return _difference(
            raw_key,
            DifferenceStatus.UNDETERMINED,
            f"{header!r} could not be validated in one of the documents",
            sdist_value,
            wheel_value,
        )
    if wheel_index >= sdist_index:
        return _difference(
            raw_key,
            DifferenceStatus.ALLOWED,
            f"the wheel uses newer {header} {wheel_value!r} than the sdist's "
            f"{sdist_value!r}; build backends may emit a newer metadata version",
            sdist_value,
            wheel_value,
        )
    return _difference(
        raw_key,
        DifferenceStatus.VIOLATION,
        f"the wheel downgraded {header} from {sdist_value!r} to {wheel_value!r}",
        sdist_value,
        wheel_value,
    )


def _diff_unparsed(
    email_name: str,
    sdist_unparsed: dict[str, list[str]],
    wheel_unparsed: dict[str, list[str]],
    *,
    contract_applies: bool,
) -> list[FieldDifference]:
    """Compare fields the parser could not turn into typed metadata."""
    raw_key = _email_to_raw(email_name)
    header = _header_for(raw_key)
    sdist_value = sdist_unparsed.get(email_name)
    wheel_value = wheel_unparsed.get(email_name)
    if sdist_value == wheel_value:
        return []

    # Reordering of identical unparsed entries is still observable.
    if Counter(sdist_value or []) == Counter(wheel_value or []):
        return _difference(
            raw_key,
            DifferenceStatus.FORMAT,
            f"{header!r} entries were reordered without a value change",
            sdist_value,
            wheel_value,
        )

    # Unrecognized headers lie outside the core metadata Dynamic contract, and
    # known fields with malformed data cannot be compared semantically. Either
    # way the evidence is insufficient to claim a violation.
    if not _is_known(raw_key):
        detail = (
            f"{header!r} is not a recognized core metadata field, so it is not "
            "covered by the Dynamic contract"
        )
    elif not contract_applies:
        detail = (
            f"{header!r} could not be parsed and the sdist metadata predates "
            "Dynamic, so the change cannot be assessed"
        )
    else:
        detail = (
            f"{header!r} contains data that could not be parsed, so the change "
            "cannot be assessed"
        )
    return _difference(
        raw_key,
        DifferenceStatus.UNDETERMINED,
        detail,
        sdist_value,
        wheel_value,
    )


def _diff_presence(
    raw_key: str,
    sdist_raw: RawMetadata,
    wheel_raw: RawMetadata,
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
) -> list[FieldDifference]:
    header = _header_for(raw_key)
    sdist_value = sdist_raw.get(raw_key)
    wheel_value = wheel_raw.get(raw_key)
    sdist_present = bool(sdist_value)
    if wheel_value is not None and not sdist_value:
        if raw_key == "requires_dist":
            detail = "Requires-Dist dependencies were added to an empty dependency set"
        else:
            detail = (
                f"{header!r} was added to the wheel without appearing in the "
                "sdist metadata"
            )
    elif raw_key == "requires_dist":
        detail = "all Requires-Dist dependencies were removed"
    else:
        detail = f"{header!r} present in the sdist metadata was removed"
    return _difference(
        raw_key,
        _presence_status(
            raw_key,
            dynamic=dynamic,
            contract_applies=contract_applies,
            sdist_value_present=sdist_present,
        ),
        detail,
        sdist_value,
        wheel_value,
    )


def _diff_known_field(
    raw_key: str,
    sdist: Metadata,
    wheel: Metadata,
    sdist_raw: RawMetadata,
    wheel_raw: RawMetadata,
    *,
    dynamic: frozenset[str] | None,
    contract_applies: bool,
) -> list[FieldDifference]:
    sdist_present = bool(sdist_raw.get(raw_key))
    wheel_present = bool(wheel_raw.get(raw_key))
    if sdist_present != wheel_present:
        return _diff_presence(
            raw_key,
            sdist_raw,
            wheel_raw,
            dynamic=dynamic,
            contract_applies=contract_applies,
        )

    if not _valid_field(sdist, raw_key) or not _valid_field(wheel, raw_key):
        header = _header_for(raw_key)
        return _difference(
            raw_key,
            DifferenceStatus.UNDETERMINED,
            f"{header!r} could not be validated in one of the documents, so the "
            "change cannot be assessed",
            sdist_raw.get(raw_key),
            wheel_raw.get(raw_key),
        )

    if raw_key == "requires_dist":
        return _diff_requires_dist(
            sdist,
            wheel,
            sdist_raw,
            wheel_raw,
            dynamic=dynamic,
            contract_applies=contract_applies,
        )
    if raw_key == "project_urls":
        sdist_urls = typing.cast("dict[str, str]", typing.cast(Any, sdist_raw)[raw_key])
        wheel_urls = typing.cast("dict[str, str]", typing.cast(Any, wheel_raw)[raw_key])
        return _diff_project_urls(
            raw_key,
            sdist_urls,
            wheel_urls,
            dynamic=dynamic,
            contract_applies=contract_applies,
        )
    if raw_key in _ORDER_INSENSITIVE_LIST_FIELDS or raw_key == "keywords":
        normalize = utils.canonicalize_name if raw_key == "provides_extra" else None
        sdist_list = typing.cast("list[str]", typing.cast(Any, sdist_raw)[raw_key])
        wheel_list = typing.cast("list[str]", typing.cast(Any, wheel_raw)[raw_key])
        return _diff_order_insensitive_list(
            raw_key,
            sdist_list,
            wheel_list,
            dynamic=dynamic,
            contract_applies=contract_applies,
            normalize=normalize,
        )
    return _diff_scalar(
        raw_key,
        sdist,
        wheel,
        sdist_raw,
        wheel_raw,
        dynamic=dynamic,
        contract_applies=contract_applies,
    )


def diff_metadata(
    sdist_pkg_info: bytes | str,
    wheel_metadata: bytes | str,
) -> MetadataDifferenceReport:
    """Compare sdist ``PKG-INFO`` against wheel ``METADATA``.

    Both arguments are the raw, already-built email-header documents. No build
    code is executed: the documents are only parsed with
    :func:`~packaging.metadata.parse_email` and compared field by field.

    The comparison honors the ``Dynamic`` semantics of the *sdist's*
    ``Metadata-Version``:

    * With Metadata 2.2 or newer, a field not listed in ``Dynamic`` must not be
      added, removed, or changed in the wheel, while ``Name``, ``Version`` and
      ``Metadata-Version`` stay constrained regardless of ``Dynamic``.
    * With older metadata versions the ``Dynamic`` mechanism does not exist, so
      changes cannot be called contract violations; they are reported as
      :attr:`DifferenceStatus.UNDETERMINED` with the evidence scope explained.

    Field-specific semantics are used throughout: name normalization, PEP 440
    version spelling, requirement parsing, marker handling and repeat-field
    ordering are distinguished rather than comparing raw text.
    """
    sdist_raw, sdist_unparsed = parse_email(sdist_pkg_info)
    wheel_raw, wheel_unparsed = parse_email(wheel_metadata)

    # Validation is intentionally lazy per attribute (validate=False), so one
    # malformed field does not prevent the rest of the comparison.
    sdist = Metadata.from_raw(sdist_raw, validate=False)
    wheel = Metadata.from_raw(wheel_raw, validate=False)

    contract_applies = _supports_dynamic(sdist)
    dynamic = _dynamic_fields(sdist) if contract_applies else None

    differences: list[FieldDifference] = []
    differences.extend(_diff_metadata_version(sdist, wheel, sdist_raw, wheel_raw))

    # Headers routed to the unparsed bucket on either side (malformed data or
    # a repeated single-value field) are compared in one place below, so they
    # are skipped here to avoid reporting the same field twice.
    unparsed_raw_keys = frozenset(
        _email_to_raw(email_name)
        for email_name in frozenset(sdist_unparsed) | frozenset(wheel_unparsed)
    )
    for raw_key in sorted(frozenset(sdist_raw) | frozenset(wheel_raw)):
        if (
            raw_key == "metadata_version"
            or not _is_known(raw_key)
            or raw_key in unparsed_raw_keys
        ):
            # Unknown headers are routed to the unparsed dict by parse_email.
            continue
        differences.extend(
            _diff_known_field(
                raw_key,
                sdist,
                wheel,
                sdist_raw,
                wheel_raw,
                dynamic=dynamic,
                contract_applies=contract_applies,
            )
        )

    for email_name in sorted(frozenset(sdist_unparsed) | frozenset(wheel_unparsed)):
        differences.extend(
            _diff_unparsed(
                email_name,
                sdist_unparsed,
                wheel_unparsed,
                contract_applies=contract_applies,
            )
        )

    return MetadataDifferenceReport(
        sdist=sdist,
        wheel=wheel,
        differences=tuple(differences),
        dynamic_contract_applies=contract_applies,
    )
