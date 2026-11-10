# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

"""Property tests for ``difference_witness``.

A witness must be a real member under ``contains`` on both original ranges:
every ``added`` value is in the new range and out of the old one, and every
``removed`` value the reverse. The converse completeness rule is that an empty
structural difference with no literal or policy split returns ``None`` rather
than inventing a value. Soundness is checked on both the plain strategy (bounds
only) and the rich strategy (``===`` literals, locals, epochs, wildcards,
pre/post/dev), and with every pre-release policy combination.
"""

from __future__ import annotations

import pytest
from hypothesis import given

from packaging.ranges import RangeDifference, VersionRange, difference_witness

from .strategies import (
    SETTINGS,
    rich_specifier_sets,
    specifier_sets,
)

pytestmark = pytest.mark.property


def _assert_sound(
    old: VersionRange,
    new: VersionRange,
    result: RangeDifference,
    prereleases: bool | None,
) -> None:
    if result.added is not None:
        assert new.contains(result.added, prereleases)
        assert not old.contains(result.added, prereleases)
    if result.removed is not None:
        assert old.contains(result.removed, prereleases)
        assert not new.contains(result.removed, prereleases)


@given(a=specifier_sets(), b=specifier_sets())
@SETTINGS
def test_witness_is_sound_plain(a: object, b: object) -> None:
    old, new = a.to_range(), b.to_range()  # type: ignore[attr-defined]
    result = difference_witness(old, new)
    _assert_sound(old, new, result, None)


@given(
    a=rich_specifier_sets(include_arbitrary=True),
    b=rich_specifier_sets(include_arbitrary=True),
)
@SETTINGS
def test_witness_is_sound_rich(a: object, b: object) -> None:
    old, new = a.to_range(), b.to_range()  # type: ignore[attr-defined]
    result = difference_witness(old, new)
    _assert_sound(old, new, result, None)


@given(a=specifier_sets(vary_prereleases=True), b=specifier_sets(vary_prereleases=True))
@SETTINGS
def test_witness_is_sound_under_every_policy(a: object, b: object) -> None:
    old, new = a.to_range(), b.to_range()  # type: ignore[attr-defined]
    for explicit in (None, True, False):
        result = difference_witness(old, new, explicit)
        _assert_sound(old, new, result, explicit)


@given(a=specifier_sets(), b=specifier_sets())
@SETTINGS
def test_equal_ranges_never_yield_a_witness(a: object, b: object) -> None:
    old, new = a.to_range(), b.to_range()  # type: ignore[attr-defined]
    if old == new:
        assert difference_witness(old, new) == RangeDifference(None, None)


@given(a=specifier_sets())
@SETTINGS
def test_self_comparison_is_empty_conclusion(a: object) -> None:
    source = a.to_range()  # type: ignore[attr-defined]
    assert difference_witness(source, source) == RangeDifference(None, None)


@given(a=rich_specifier_sets(include_arbitrary=True), b=rich_specifier_sets())
@SETTINGS
def test_witness_directions_are_consistent(a: object, b: object) -> None:
    old, new = a.to_range(), b.to_range()  # type: ignore[attr-defined]
    forward = difference_witness(old, new)
    backward = difference_witness(new, old)
    # Reversing the operands swaps the two witness fields.
    assert RangeDifference(
        added=backward.removed, removed=backward.added
    ) == forward
