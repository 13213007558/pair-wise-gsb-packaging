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


def test_select_or_and_with_combination() -> None:
    policy = LicensePolicy(
        allowed_licenses={"mit", "apache-2.0"},
        allowed_with={("gpl-2.0-only", "classpath-exception-2.0")},
    )
    selection = select_license_expression(
        "(MIT OR Apache-2.0) AND (GPL-2.0-only WITH Classpath-exception-2.0)",
        policy,
    )
    assert selection.status == "acceptable"
    assert selection.acceptable
    assert selection.expression == "MIT AND (GPL-2.0-only WITH Classpath-exception-2.0)"
    assert selection.licenses == {"MIT", "GPL-2.0-only"}
    assert selection.exceptions == {"Classpath-exception-2.0"}
    assert selection.unknown == frozenset()
    assert selection.blockers == ()
    assert (
        selection.normalized_expression
        == "(MIT OR Apache-2.0) AND (GPL-2.0-only WITH Classpath-exception-2.0)"
    )


def test_select_and_requires_every_branch() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT"})
    selection = select_license_expression("MIT AND BSD-2-Clause", policy)
    assert selection.status == "blocked"
    assert not selection.acceptable
    assert selection.expression is None
    assert selection.blockers == ("BSD-2-Clause",)


def test_select_allowed_id_anywhere_is_not_enough() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT"})
    selection = select_license_expression(
        "(MIT OR LicenseRef-A) AND LicenseRef-B", policy
    )
    assert selection.status == "blocked"
    assert selection.blockers == ("LicenseRef-B",)


def test_select_or_requires_a_complete_branch() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT", "BSD-2-Clause"})
    selection = select_license_expression(
        "(MIT AND Apache-2.0) OR BSD-2-Clause", policy
    )
    assert selection.status == "acceptable"
    assert selection.expression == "BSD-2-Clause"
    assert selection.licenses == {"BSD-2-Clause"}


def test_select_or_reports_whole_group_as_minimal_blocker() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT"})
    selection = select_license_expression(
        "(GPL-3.0-only OR BSD-3-Clause) AND MIT", policy
    )
    assert selection.status == "blocked"
    assert selection.blockers == ("(GPL-3.0-only OR BSD-3-Clause)",)


def test_select_multiple_blockers_in_source_order() -> None:
    policy = LicensePolicy(allowed_licenses=set())
    selection = select_license_expression(
        "LicenseRef-A AND (GPL-3.0-only OR BSD-3-Clause) AND MIT", policy
    )
    assert selection.status == "blocked"
    assert selection.blockers == (
        "LicenseRef-A",
        "(GPL-3.0-only OR BSD-3-Clause)",
        "MIT",
    )


def test_select_preference_breaks_ties() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT", "Apache-2.0"}, preference=("MIT",))
    selection = select_license_expression("Apache-2.0 OR MIT", policy)
    assert selection.status == "acceptable"
    assert selection.expression == "MIT"


def test_select_without_preference_keeps_leftmost() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT", "Apache-2.0"})
    selection = select_license_expression("Apache-2.0 OR MIT", policy)
    assert selection.status == "acceptable"
    assert selection.expression == "Apache-2.0"


def test_select_with_combination_must_be_explicitly_allowed() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT"})
    selection = select_license_expression("MIT WITH Classpath-exception-2.0", policy)
    assert selection.status == "blocked"
    assert selection.blockers == ("MIT WITH Classpath-exception-2.0",)


def test_select_licenseref_allowed_explicitly() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT", "LicenseRef-Proprietary"})
    selection = select_license_expression("LicenseRef-Proprietary AND MIT", policy)
    assert selection.status == "acceptable"
    assert selection.licenses == {"LicenseRef-Proprietary", "MIT"}


def test_select_unknown_licenseref_rejected_by_default() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT"})
    selection = select_license_expression("MIT AND LicenseRef-Custom", policy)
    assert selection.status == "blocked"
    assert selection.blockers == ("LicenseRef-Custom",)


def test_select_unknown_licenseref_flagged() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT"}, on_unknown="flag")
    selection = select_license_expression("MIT AND LicenseRef-Custom", policy)
    assert selection.status == "unknown"
    assert not selection.acceptable
    assert selection.expression == "MIT AND LicenseRef-Custom"
    assert selection.unknown == {"LicenseRef-Custom"}
    assert selection.licenses == {"MIT", "LicenseRef-Custom"}


def test_select_prefers_known_branch_over_unknown_branch() -> None:
    policy = LicensePolicy(allowed_licenses={"MIT"}, on_unknown="flag")
    selection = select_license_expression("LicenseRef-Custom OR MIT", policy)
    assert selection.status == "acceptable"
    assert selection.expression == "MIT"


def test_select_policy_ids_are_normalized() -> None:
    policy = LicensePolicy(
        allowed_licenses={"mit"},
        allowed_with={("gpl-2.0-only", "classpath-exception-2.0")},
    )
    assert policy.allowed_licenses == {"MIT"}
    assert policy.allowed_with == {("GPL-2.0-only", "Classpath-exception-2.0")}


@pytest.mark.parametrize(
    ("policy_kwargs", "match"),
    [
        ({"allowed_licenses": {"Not-A-License"}}, "Unknown license in policy"),
        ({"allowed_licenses": {"LicenseRef-"}}, "Invalid licenseref in policy"),
        (
            {"allowed_with": {("MIT", "not-an-exception")}},
            "Unknown license exception in policy",
        ),
        ({"preference": ("Not-A-License",)}, "Unknown license in policy"),
        ({"on_unknown": "allow"}, "Invalid on_unknown value"),
    ],
)
def test_select_policy_validation(policy_kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        LicensePolicy(**policy_kwargs)


def test_select_invalid_expression_raises() -> None:
    with pytest.raises(InvalidLicenseExpression):
        select_license_expression("(mit", LicensePolicy())
