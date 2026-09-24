#######################################################################################
#
# Adapted from:
#  https://github.com/pypa/hatch/blob/5352e44/backend/src/hatchling/licenses/parse.py
#
# MIT License
#
# Copyright (c) 2017-present Ofek Lev <oss@ofek.dev>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be included in all copies
# or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
# CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
# OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
#
# With additional allowance of arbitrary `LicenseRef-` identifiers, not just
# `LicenseRef-Public-Domain` and `LicenseRef-Proprietary`.
#
#######################################################################################
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NewType, cast

from ._spdx import EXCEPTIONS, LICENSES

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "InvalidLicenseExpression",
    "LicensePolicy",
    "LicenseSelection",
    "NormalizedLicenseExpression",
    "canonicalize_license_expression",
    "select_license_expression",
]


# Simple __dir__ implementation since there are no public submodules
def __dir__() -> list[str]:
    return __all__


license_ref_allowed = re.compile("^[A-Za-z0-9.-]+$")

NormalizedLicenseExpression = NewType("NormalizedLicenseExpression", str)
"""
A :class:`typing.NewType` of :class:`str`, representing a normalized
License-Expression.
"""


class InvalidLicenseExpression(ValueError):
    """Raised when a license-expression string is invalid

    >>> from packaging.licenses import canonicalize_license_expression
    >>> canonicalize_license_expression("invalid")
    Traceback (most recent call last):
        ...
    packaging.licenses.InvalidLicenseExpression: Unknown license: 'invalid'
    """


def canonicalize_license_expression(
    raw_license_expression: str,
) -> NormalizedLicenseExpression:
    """
    This function takes a valid License-Expression, and returns the normalized
    form of it.

    The return type is typed as :class:`NormalizedLicenseExpression`. This
    allows type checkers to help require that a string has passed through this
    function before use.

    :param str raw_license_expression: The License-Expression to canonicalize.
    :raises InvalidLicenseExpression: If the License-Expression is invalid due to an
        invalid/unknown license identifier or invalid syntax.

    .. doctest::

        >>> from packaging.licenses import canonicalize_license_expression
        >>> canonicalize_license_expression("mit")
        'MIT'
        >>> canonicalize_license_expression("mit and (apache-2.0 or bsd-2-clause)")
        'MIT AND (Apache-2.0 OR BSD-2-Clause)'
        >>> canonicalize_license_expression("(mit")
        Traceback (most recent call last):
          ...
        InvalidLicenseExpression: Invalid license expression: '(mit'
        >>> canonicalize_license_expression("Use-it-after-midnight")
        Traceback (most recent call last):
          ...
        InvalidLicenseExpression: Unknown license: 'Use-it-after-midnight'
    """
    if not raw_license_expression:
        message = f"Invalid license expression: {raw_license_expression!r}"
        raise InvalidLicenseExpression(message)

    # Pad any parentheses so tokenization can be achieved by merely splitting on
    # whitespace.
    license_expression = raw_license_expression.replace("(", " ( ").replace(")", " ) ")
    licenseref_prefix = "LicenseRef-"
    license_refs = {
        ref.lower(): "LicenseRef-" + ref[len(licenseref_prefix) :]
        for ref in license_expression.split()
        if ref.lower().startswith(licenseref_prefix.lower())
    }

    # Normalize to lower case so we can look up licenses/exceptions
    # and so boolean operators are Python-compatible.
    license_expression = license_expression.lower()

    tokens = license_expression.split()

    # Rather than implementing a parenthesis/boolean logic parser, create an
    # expression that Python can parse. Everything that is not involved with the
    # grammar itself is replaced with the placeholder `False` and the resultant
    # expression should become a valid Python expression.
    python_tokens = []
    for token in tokens:
        if token not in {"or", "and", "with", "(", ")"}:
            python_tokens.append("False")
        elif token == "with":
            python_tokens.append("or")
        elif (
            token == "("
            and python_tokens
            and python_tokens[-1] not in {"or", "and", "("}
        ) or (token == ")" and python_tokens and python_tokens[-1] == "("):
            message = f"Invalid license expression: {raw_license_expression!r}"
            raise InvalidLicenseExpression(message)
        else:
            python_tokens.append(token)

    python_expression = " ".join(python_tokens)
    try:
        compile(python_expression, "", "eval")
    except SyntaxError:
        message = f"Invalid license expression: {raw_license_expression!r}"
        raise InvalidLicenseExpression(message) from None

    # Take a final pass to check for unknown licenses/exceptions.
    normalized_tokens = []
    last_license_start = False
    for index, token in enumerate(tokens):
        if token in {"or", "and", "with", "(", ")"}:
            if token == "with" and (
                not last_license_start
                or index + 1 == len(tokens)
                or tokens[index + 1] in {"or", "and", "with", "(", ")"}
            ):
                message = f"Invalid license expression: {raw_license_expression!r}"
                raise InvalidLicenseExpression(message)
            normalized_tokens.append(token.upper())
            last_license_start = False
            continue

        if normalized_tokens and normalized_tokens[-1] == "WITH":
            if token not in EXCEPTIONS:
                message = f"Unknown license exception: {token!r}"
                raise InvalidLicenseExpression(message)

            normalized_tokens.append(EXCEPTIONS[token]["id"])
            last_license_start = False
        else:
            if token.endswith("+"):
                final_token = token[:-1]
                suffix = "+"
            else:
                final_token = token
                suffix = ""

            if final_token.startswith("licenseref-"):
                license_ref_id = final_token[len("licenseref-") :]
                if suffix or not license_ref_allowed.match(license_ref_id):
                    message = f"Invalid licenseref: {token!r}"
                    raise InvalidLicenseExpression(message)
                normalized_tokens.append(license_refs[final_token])
            else:
                if final_token not in LICENSES:
                    message = f"Unknown license: {final_token!r}"
                    raise InvalidLicenseExpression(message)
                normalized_tokens.append(LICENSES[final_token]["id"] + suffix)
            last_license_start = True

    normalized_expression = " ".join(normalized_tokens)

    return cast(
        "NormalizedLicenseExpression",
        normalized_expression.replace("( ", "(").replace(" )", ")"),
    )


def _canonical_policy_license_id(license_id: str) -> str:
    """Normalize a license ID supplied in a policy to its canonical form."""
    suffix = "+" if license_id.endswith("+") else ""
    base = license_id[:-1] if suffix else license_id
    if base.lower().startswith("licenseref-"):
        ref = base[len("licenseref-") :]
        if suffix or not ref or not license_ref_allowed.match(ref):
            message = f"Invalid licenseref in policy: {license_id!r}"
            raise ValueError(message)
        return "LicenseRef-" + ref
    key = base.lower()
    if key not in LICENSES:
        message = f"Unknown license in policy: {license_id!r}"
        raise ValueError(message)
    return LICENSES[key]["id"] + suffix


def _canonical_policy_exception_id(exception_id: str) -> str:
    """Normalize an exception ID supplied in a policy to its canonical form."""
    key = exception_id.lower()
    if key not in EXCEPTIONS:
        message = f"Unknown license exception in policy: {exception_id!r}"
        raise ValueError(message)
    return EXCEPTIONS[key]["id"]


@dataclass(frozen=True)
class LicensePolicy:
    """A caller-provided policy for selecting an acceptable license branch.

    The policy carries no built-in legal judgement: a license or
    ``WITH`` exception combination is acceptable only when explicitly
    listed here.

    :param allowed_licenses: License IDs (SPDX IDs or ``LicenseRef-*``)
        that are acceptable on their own. Case-insensitive for SPDX IDs;
        normalized to canonical form.
    :param allowed_with: ``(license_id, exception_id)`` pairs that are
        acceptable as ``license WITH exception`` combinations. Allowing a
        license on its own does not allow any ``WITH`` combination and
        vice versa.
    :param preference: License IDs in decreasing order of preference, used
        to choose between equally acceptable ``OR`` branches. Branches
        whose licenses appear earlier are preferred; unlisted IDs rank
        last and earlier (leftmost) branches win ties.
    :param on_unknown: How to treat items the policy does not know about
        (``LicenseRef-*`` IDs not explicitly allowed). ``"reject"`` treats
        them as unacceptable; ``"flag"`` lets them be selected but reports
        them in :attr:`LicenseSelection.unknown` with an ``"unknown"``
        status. Unknown items are never silently treated as allowed.
    """

    allowed_licenses: Iterable[str] = ()
    allowed_with: Iterable[tuple[str, str]] = ()
    preference: Iterable[str] = ()
    on_unknown: Literal["reject", "flag"] = "reject"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_licenses",
            frozenset(
                _canonical_policy_license_id(license_id)
                for license_id in self.allowed_licenses
            ),
        )
        object.__setattr__(
            self,
            "allowed_with",
            frozenset(
                (
                    _canonical_policy_license_id(license_id),
                    _canonical_policy_exception_id(exception_id),
                )
                for license_id, exception_id in self.allowed_with
            ),
        )
        object.__setattr__(
            self,
            "preference",
            tuple(
                _canonical_policy_license_id(license_id)
                for license_id in self.preference
            ),
        )
        if self.on_unknown not in {"reject", "flag"}:
            message = f"Invalid on_unknown value: {self.on_unknown!r}"
            raise ValueError(message)


@dataclass(frozen=True)
class LicenseSelection:
    """The outcome of :func:`select_license_expression`.

    ``expression`` and ``blockers`` are slices of the normalized source
    expression, preserving the structure (including parenthesization) of
    the original expression rather than re-serializing a canonical tree.
    """

    status: Literal["acceptable", "unknown", "blocked"]
    """``"acceptable"`` if a fully allowed branch was selected,
    ``"unknown"`` if the selected branch relies on items flagged as
    unknown by the policy, or ``"blocked"`` if no branch is selectable."""

    expression: str | None
    """The selected subexpression, or ``None`` when blocked."""

    licenses: frozenset[str]
    """License IDs required by the selected branch."""

    exceptions: frozenset[str]
    """Exception IDs required by the selected branch (via ``WITH``)."""

    unknown: frozenset[str]
    """Unknown items the selected branch relies on (only with
    ``on_unknown="flag"``)."""

    blockers: tuple[str, ...]
    """Minimal subexpressions of the source that block selection, in
    source order. Empty unless the selection is blocked."""

    normalized_expression: str
    """The normalized source expression the selection was made from."""

    @property
    def acceptable(self) -> bool:
        """Whether a fully allowed branch was selected."""
        return self.status == "acceptable"


@dataclass
class _ExpressionNode:
    kind: str  # "license", "and", or "or"
    start: int  # index of the first token of this node
    end: int  # index one past the last token of this node
    children: list[_ExpressionNode] = field(default_factory=list)
    license_id: str | None = None
    exception_id: str | None = None


def _parse_or(tokens: list[str], pos: int) -> tuple[_ExpressionNode, int]:
    child, pos = _parse_and(tokens, pos)
    children = [child]
    while pos < len(tokens) and tokens[pos] == "OR":
        child, pos = _parse_and(tokens, pos + 1)
        children.append(child)
    if len(children) == 1:
        return children[0], pos
    return (
        _ExpressionNode("or", children[0].start, children[-1].end, children),
        pos,
    )


def _parse_and(tokens: list[str], pos: int) -> tuple[_ExpressionNode, int]:
    child, pos = _parse_with(tokens, pos)
    children = [child]
    while pos < len(tokens) and tokens[pos] == "AND":
        child, pos = _parse_with(tokens, pos + 1)
        children.append(child)
    if len(children) == 1:
        return children[0], pos
    return (
        _ExpressionNode("and", children[0].start, children[-1].end, children),
        pos,
    )


def _parse_with(tokens: list[str], pos: int) -> tuple[_ExpressionNode, int]:
    node, pos = _parse_atom(tokens, pos)
    if pos < len(tokens) and tokens[pos] == "WITH":
        # canonicalize_license_expression guarantees WITH only follows a
        # license token and is followed by a known exception ID.
        node = _ExpressionNode(
            "license",
            node.start,
            pos + 2,
            license_id=node.license_id,
            exception_id=tokens[pos + 1],
        )
        pos += 2
    return node, pos


def _parse_atom(tokens: list[str], pos: int) -> tuple[_ExpressionNode, int]:
    if tokens[pos] == "(":
        start = pos
        node, pos = _parse_or(tokens, pos + 1)
        # Keep the parentheses in the node's span so reported slices
        # preserve the source structure.
        node.start = start
        node.end = pos + 1
        return node, pos + 1
    return _ExpressionNode("license", pos, pos + 1, license_id=tokens[pos]), pos + 1


def _source_slice(tokens: list[str], node: _ExpressionNode) -> str:
    text = " ".join(tokens[node.start : node.end])
    return text.replace("( ", "(").replace(" )", ")")


@dataclass
class _Evaluation:
    status: str  # "ok", "unknown", or "fail"
    licenses: frozenset[str]
    exceptions: frozenset[str]
    unknown: frozenset[str]
    choice: str | None
    blockers: tuple[str, ...]


def _evaluate(
    node: _ExpressionNode, policy: LicensePolicy, tokens: list[str]
) -> _Evaluation:
    if node.kind == "license":
        return _evaluate_license(node, policy, tokens)

    evaluations = [_evaluate(child, policy, tokens) for child in node.children]

    if node.kind == "and":
        # AND requires every branch to be satisfied; each failing branch
        # contributes its own minimal blockers.
        if any(evaluation.status == "fail" for evaluation in evaluations):
            return _Evaluation(
                "fail",
                frozenset(),
                frozenset(),
                frozenset(),
                None,
                tuple(
                    blocker
                    for evaluation in evaluations
                    for blocker in evaluation.blockers
                ),
            )
        return _Evaluation(
            "unknown"
            if any(evaluation.status == "unknown" for evaluation in evaluations)
            else "ok",
            frozenset().union(*(evaluation.licenses for evaluation in evaluations)),
            frozenset().union(*(evaluation.exceptions for evaluation in evaluations)),
            frozenset().union(*(evaluation.unknown for evaluation in evaluations)),
            " AND ".join(cast("str", evaluation.choice) for evaluation in evaluations),
            (),
        )

    # OR requires one complete branch; pick the most preferred viable one.
    viable = [
        (index, evaluation)
        for index, evaluation in enumerate(evaluations)
        if evaluation.status != "fail"
    ]
    if not viable:
        return _Evaluation(
            "fail",
            frozenset(),
            frozenset(),
            frozenset(),
            None,
            (_source_slice(tokens, node),),
        )
    preference_index = {
        license_id: index for index, license_id in enumerate(policy.preference)
    }

    def sort_key(item: tuple[int, _Evaluation]) -> tuple[int, int, int, int]:
        index, evaluation = item
        rank = min(
            (
                preference_index.get(license_id, len(preference_index))
                for license_id in evaluation.licenses
            ),
            default=len(preference_index),
        )
        return (
            0 if evaluation.status == "ok" else 1,
            rank,
            len(evaluation.licenses) + len(evaluation.exceptions),
            index,
        )

    return min(viable, key=sort_key)[1]


def _evaluate_license(
    node: _ExpressionNode, policy: LicensePolicy, tokens: list[str]
) -> _Evaluation:
    license_id = cast("str", node.license_id)
    exception_id = node.exception_id
    source = _source_slice(tokens, node)
    licenses = frozenset({license_id})
    exceptions = frozenset({exception_id}) if exception_id else frozenset()

    if exception_id is None:
        allowed = license_id in policy.allowed_licenses
    else:
        allowed = (license_id, exception_id) in policy.allowed_with
    if allowed:
        return _Evaluation("ok", licenses, exceptions, frozenset(), source, ())

    # A LicenseRef the policy does not explicitly allow is unknown to the
    # policy; it is rejected or flagged, never silently allowed.
    if license_id.startswith("LicenseRef-") and policy.on_unknown == "flag":
        return _Evaluation(
            "unknown", licenses, exceptions, frozenset({license_id}), source, ()
        )
    return _Evaluation("fail", frozenset(), frozenset(), frozenset(), None, (source,))


def select_license_expression(
    raw_license_expression: str, policy: LicensePolicy
) -> LicenseSelection:
    """Select an acceptable branch of a License-Expression under a policy.

    ``AND`` requires every branch to be satisfied, while ``OR`` requires
    one complete branch; a single allowed ID appearing anywhere in the
    expression is not sufficient. Among equally acceptable ``OR``
    branches, the caller-provided :attr:`LicensePolicy.preference` order
    breaks ties.

    :param str raw_license_expression: The License-Expression to evaluate.
        It is first normalized with :func:`canonicalize_license_expression`.
    :param LicensePolicy policy: The caller-provided policy describing
        which license IDs and ``WITH`` exception combinations are allowed.
    :raises InvalidLicenseExpression: If the License-Expression is invalid.
    :return: A :class:`LicenseSelection` describing the selected branch
        and the license/exception IDs it requires, or the minimal
        blocking subexpressions when no branch is selectable.

    .. doctest::

        >>> from packaging.licenses import LicensePolicy, select_license_expression
        >>> policy = LicensePolicy(
        ...     allowed_licenses={"mit", "apache-2.0"},
        ...     allowed_with={("gpl-2.0-only", "classpath-exception-2.0")},
        ...     preference=("MIT", "Apache-2.0"),
        ... )
        >>> selection = select_license_expression(
        ...     "(Apache-2.0 OR MIT) AND (GPL-2.0-only WITH Classpath-exception-2.0)",
        ...     policy,
        ... )
        >>> selection.status
        'acceptable'
        >>> selection.expression
        'MIT AND (GPL-2.0-only WITH Classpath-exception-2.0)'
        >>> sorted(selection.licenses)
        ['GPL-2.0-only', 'MIT']
        >>> sorted(selection.exceptions)
        ['Classpath-exception-2.0']
        >>> blocked = select_license_expression(
        ...     "MIT AND LicenseRef-Proprietary", policy
        ... )
        >>> blocked.status
        'blocked'
        >>> blocked.blockers
        ('LicenseRef-Proprietary',)
    """
    normalized = canonicalize_license_expression(raw_license_expression)
    tokens = normalized.replace("(", " ( ").replace(")", " ) ").split()
    node, pos = _parse_or(tokens, 0)
    if pos != len(tokens):  # pragma: no cover - guarded by canonicalization
        message = f"Invalid license expression: {raw_license_expression!r}"
        raise InvalidLicenseExpression(message)

    evaluation = _evaluate(node, policy, tokens)
    status = {"ok": "acceptable", "unknown": "unknown", "fail": "blocked"}[
        evaluation.status
    ]
    return LicenseSelection(
        status=cast("Literal['acceptable', 'unknown', 'blocked']", status),
        expression=evaluation.choice,
        licenses=evaluation.licenses,
        exceptions=evaluation.exceptions,
        unknown=evaluation.unknown,
        blockers=evaluation.blockers,
        normalized_expression=normalized,
    )
