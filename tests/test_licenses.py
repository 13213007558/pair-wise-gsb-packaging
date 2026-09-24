import pytest

from packaging.licenses import (
    InvalidLicenseExpression,
    LicensePolicy,
    canonicalize_license_expression,
    select_license_expression,
)
from packaging.licenses._spdx import EXCEPTIONS, LICENSES


def test_licenses() -> None:
    for license_id in LICENSES:
        assert license_id == license_id.lower()


def test_exceptions() -> None:
    for exception_id in EXCEPTIONS:
        assert exception_id == exception_id.lower()


def test_licenseref_plus_suffix_is_invalid() -> None:
    with pytest.raises(InvalidLicenseExpression, match="Invalid licenseref"):
        canonicalize_license_expression("LicenseRef-Foo+")


@pytest.mark.parametrize(
    "license_expression",
    [
        "MIT WITH Classpath-exception-2.0 WITH GCC-exception-3.1",
        "(MIT) WITH Classpath-exception-2.0",
        "(MIT OR Apache-2.0) WITH Classpath-exception-2.0",
        "LicenseRef-",
    ],
)
def test_invalid_spdx_with_or_licenseref_forms(license_expression: str) -> None:
    with pytest.raises(InvalidLicenseExpression):
        canonicalize_license_expression(license_expression)


class TestSelectLicenseExpression:
    def test_and_or_with_combination_accepted(self) -> None:
        policy = LicensePolicy(
            allowed_licenses={"MIT", "BSD-2-Clause"},
            allowed_exceptions={"Classpath-exception-2.0"},
        )
        selection = select_license_expression(
            "(MIT OR Apache-2.0) AND (BSD-2-Clause WITH Classpath-exception-2.0)",
            policy,
        )
        assert selection.accepted
        assert selection.licenses == {"MIT", "BSD-2-Clause"}
        assert selection.exceptions == {"Classpath-exception-2.0"}
        assert selection.license_refs == frozenset()
        assert selection.unknown == frozenset()
        assert selection.sources == (
            "MIT",
            "(BSD-2-Clause WITH Classpath-exception-2.0)",
        )
        assert (
            selection.selection == "MIT AND (BSD-2-Clause WITH Classpath-exception-2.0)"
        )
        assert selection.blocker is None

    def test_and_requires_both_sides(self) -> None:
        # A single allowed ID anywhere in the expression is not sufficient.
        policy = LicensePolicy(allowed_licenses={"MIT"})
        selection = select_license_expression("MIT AND GPL-2.0-only", policy)
        assert not selection.accepted
        assert selection.blocker == "GPL-2.0-only"
        assert selection.blocked_by == {"GPL-2.0-only"}
        assert selection.selection is None

    def test_or_selects_one_complete_branch(self) -> None:
        policy = LicensePolicy(allowed_licenses={"MIT", "Apache-2.0"})
        selection = select_license_expression(
            "GPL-2.0-only OR (MIT AND Apache-2.0)", policy
        )
        assert selection.accepted
        assert selection.licenses == {"MIT", "Apache-2.0"}
        assert selection.sources == ("MIT", "Apache-2.0")
        assert selection.selection == "MIT AND Apache-2.0"

    def test_or_branch_must_be_complete(self) -> None:
        # Neither OR branch is fully acceptable, so the expression blocks.
        policy = LicensePolicy(allowed_licenses={"MIT"})
        selection = select_license_expression(
            "MIT AND Apache-2.0 OR BSD-2-Clause", policy
        )
        assert not selection.accepted
        # The minimal blocker is the single disallowed leaf.
        assert selection.blocker == "Apache-2.0"
        assert selection.blocked_by == {"Apache-2.0"}

    def test_with_exception_must_be_allowed(self) -> None:
        policy = LicensePolicy(
            allowed_licenses={"MIT", "BSD-2-Clause"},
            allowed_exceptions=set(),
        )
        selection = select_license_expression(
            "(MIT OR Apache-2.0) AND (BSD-2-Clause WITH Classpath-exception-2.0)",
            policy,
        )
        assert not selection.accepted
        assert selection.blocker == "(BSD-2-Clause WITH Classpath-exception-2.0)"
        assert selection.blocked_by == {"Classpath-exception-2.0"}

    def test_preference_breaks_ties(self) -> None:
        policy = LicensePolicy(
            allowed_licenses={"MIT", "Apache-2.0"},
            preference=("Apache-2.0", "MIT"),
        )
        selection = select_license_expression("MIT OR Apache-2.0", policy)
        assert selection.accepted
        assert selection.licenses == {"Apache-2.0"}
        assert selection.selection == "Apache-2.0"

    def test_preference_is_case_insensitive(self) -> None:
        policy = LicensePolicy(
            allowed_licenses={"mit", "apache-2.0"},
            preference=("apache-2.0",),
        )
        selection = select_license_expression("MIT OR Apache-2.0", policy)
        assert selection.accepted
        assert selection.licenses == {"Apache-2.0"}

    def test_licenseref_requires_explicit_allowance(self) -> None:
        policy = LicensePolicy(allowed_licenses={"MIT"})
        selection = select_license_expression("MIT AND LicenseRef-Proprietary", policy)
        assert not selection.accepted
        assert selection.blocker == "LicenseRef-Proprietary"
        assert selection.blocked_by == {"LicenseRef-Proprietary"}

    def test_licenseref_allowed_explicitly(self) -> None:
        policy = LicensePolicy(allowed_license_refs={"LicenseRef-Proprietary"})
        selection = select_license_expression("LicenseRef-Proprietary", policy)
        assert selection.accepted
        assert selection.license_refs == {"LicenseRef-Proprietary"}
        assert selection.licenses == frozenset()

    def test_unknown_marked_instead_of_rejected(self) -> None:
        policy = LicensePolicy(allowed_licenses={"MIT"}, reject_unknown=False)
        selection = select_license_expression("MIT AND LicenseRef-Proprietary", policy)
        assert selection.accepted
        assert selection.unknown == {"LicenseRef-Proprietary"}
        assert selection.license_refs == {"LicenseRef-Proprietary"}

    def test_plus_suffix_matches_base_id(self) -> None:
        policy = LicensePolicy(allowed_licenses={"Apache-2.0"})
        selection = select_license_expression("Apache-2.0+", policy)
        assert selection.accepted
        assert selection.licenses == {"Apache-2.0+"}

    def test_normalized_expression_is_reported(self) -> None:
        policy = LicensePolicy(allowed_licenses={"MIT"})
        selection = select_license_expression("mit", policy)
        assert selection.accepted
        assert selection.expression == "MIT"

    def test_invalid_expression_still_raises(self) -> None:
        policy = LicensePolicy(allowed_licenses={"MIT"})
        with pytest.raises(InvalidLicenseExpression):
            select_license_expression("(MIT", policy)
