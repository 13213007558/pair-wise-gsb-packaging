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

import dataclasses
import re
from typing import TYPE_CHECKING, NewType, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

from ._spdx import EXCEPTIONS, LICENSES

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


@dataclasses.dataclass(frozen=True)
class LicensePolicy:
    """A caller-provided policy describing acceptable licenses.

    The policy performs no legal judgement of its own: it simply records
    which license IDs, ``WITH`` exceptions and ``LicenseRef-`` identifiers
    the caller considers acceptable, plus how to break ties between
    equally acceptable branches.

    :param allowed_licenses: SPDX license IDs that are acceptable
        (matched case-insensitively; a trailing ``+`` in the expression is
        ignored when matching).
    :param allowed_exceptions: SPDX exception IDs that are acceptable in
        ``WITH`` clauses (matched case-insensitively).
    :param allowed_license_refs: ``LicenseRef-`` identifiers that are
        acceptable (matched case-insensitively). ``LicenseRef-`` identifiers
        are never implicitly acceptable.
    :param preference: License IDs (or ``LicenseRef-`` identifiers) in
        descending order of preference, used to choose between equally
        acceptable branches. Earlier entries win.
    :param reject_unknown: When True (default), any identifier not covered
        by the allow-lists blocks the branch it appears in. When False,
        such identifiers are reported via
        :attr:`LicenseSelection.unknown` instead of blocking.
    """

    allowed_licenses: Iterable[str] = ()
    allowed_exceptions: Iterable[str] = ()
    allowed_license_refs: Iterable[str] = ()
    preference: Iterable[str] = ()
    reject_unknown: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_licenses", frozenset(self.allowed_licenses))
        object.__setattr__(
            self, "allowed_exceptions", frozenset(self.allowed_exceptions)
        )
        object.__setattr__(
            self, "allowed_license_refs", frozenset(self.allowed_license_refs)
        )
        object.__setattr__(self, "preference", tuple(self.preference))


@dataclasses.dataclass(frozen=True)
class LicenseSelection:
    """The outcome of :func:`select_license_expression`.

    :ivar accepted: Whether an acceptable branch could be selected.
    :ivar expression: The normalized expression that was evaluated.
    :ivar licenses: License IDs required by the selected branch, as written
        in the normalized expression.
    :ivar exceptions: Exception IDs required by the selected branch.
    :ivar license_refs: ``LicenseRef-`` identifiers required by the
        selected branch.
    :ivar unknown: Identifiers in the selected branch not covered by the
        policy's allow-lists. Only populated when the policy sets
        ``reject_unknown=False``.
    :ivar sources: The leaf sub-expressions of the original expression
        that the selection was built from, in order of appearance.
    :ivar selection: The selected requirements as a single expression
        (the :ivar:`sources` joined with ``AND``), or None when rejected.
    :ivar blocker: The minimal sub-expression that blocks acceptance,
        or None when accepted.
    :ivar blocked_by: The identifiers within :ivar:`blocker` that the
        policy does not allow.
    """

    accepted: bool
    expression: NormalizedLicenseExpression
    licenses: frozenset[str] = frozenset()
    exceptions: frozenset[str] = frozenset()
    license_refs: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()
    sources: tuple[str, ...] = ()
    selection: str | None = None
    blocker: str | None = None
    blocked_by: frozenset[str] = frozenset()


@dataclasses.dataclass(frozen=True)
class _Leaf:
    text: str
    license_token: str
    exception_token: str | None
    is_license_ref: bool
    position: int


@dataclasses.dataclass(frozen=True)
class _And:
    text: str
    left: _Node
    right: _Node
    position: int


@dataclasses.dataclass(frozen=True)
class _Or:
    text: str
    left: _Node
    right: _Node
    position: int


_Node = _Leaf | _And | _Or


@dataclasses.dataclass(frozen=True)
class _Option:
    """One way of satisfying (a sub-)expression."""

    licenses: frozenset[str]
    exceptions: frozenset[str]
    license_refs: frozenset[str]
    unknown: frozenset[str]
    sources: tuple[str, ...]


def _parse_normalized_expression(expression: str) -> _Node:
    """Parse an already-normalized expression into a simple tree."""
    tokens = expression.replace("(", " ( ").replace(")", " ) ").split()
    position = 0

    def parse_or() -> _Node:
        nonlocal position
        left = parse_and()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            right = parse_and()
            left = _Or(
                text=f"{left.text} OR {right.text}",
                left=left,
                right=right,
                position=left.position,
            )
        return left

    def parse_and() -> _Node:
        nonlocal position
        left = parse_with()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            right = parse_with()
            left = _And(
                text=f"{left.text} AND {right.text}",
                left=left,
                right=right,
                position=left.position,
            )
        return left

    def parse_with() -> _Node:
        nonlocal position
        node = parse_atom()
        if position < len(tokens) and tokens[position] == "WITH":
            position += 1
            exception_token = tokens[position]
            position += 1
            assert isinstance(node, _Leaf)
            node = dataclasses.replace(
                node,
                text=f"{node.license_token} WITH {exception_token}",
                exception_token=exception_token,
            )
        return node

    def parse_atom() -> _Node:
        nonlocal position
        token = tokens[position]
        if token == "(":
            position += 1
            node = parse_or()
            position += 1  # consume ")"
            return dataclasses.replace(node, text=f"({node.text})")
        position += 1
        return _Leaf(
            text=token,
            license_token=token,
            exception_token=None,
            is_license_ref=token.startswith("LicenseRef-"),
            position=position - 1,
        )

    return parse_or()


def _base_license_id(token: str) -> str:
    return token.removesuffix("+")


def _leaf_unknown(leaf: _Leaf, policy: LicensePolicy) -> frozenset[str]:
    """Identifiers of a leaf that the policy does not explicitly allow."""
    unknown = set()
    if leaf.is_license_ref:
        allowed_refs = {ref.lower() for ref in policy.allowed_license_refs}
        if leaf.license_token.lower() not in allowed_refs:
            unknown.add(leaf.license_token)
    else:
        allowed_licenses = {id_.lower() for id_ in policy.allowed_licenses}
        if _base_license_id(leaf.license_token).lower() not in allowed_licenses:
            unknown.add(leaf.license_token)
    if leaf.exception_token is not None:
        allowed_exceptions = {id_.lower() for id_ in policy.allowed_exceptions}
        if leaf.exception_token.lower() not in allowed_exceptions:
            unknown.add(leaf.exception_token)
    return frozenset(unknown)


def _evaluate(node: _Node, policy: LicensePolicy) -> list[_Option]:
    """All policy-acceptable ways of satisfying the node."""
    if isinstance(node, _Leaf):
        unknown = _leaf_unknown(node, policy)
        if unknown and policy.reject_unknown:
            return []
        licenses: frozenset[str]
        license_refs: frozenset[str]
        if node.is_license_ref:
            licenses, license_refs = frozenset(), frozenset([node.license_token])
        else:
            licenses, license_refs = frozenset([node.license_token]), frozenset()
        exceptions = (
            frozenset([node.exception_token]) if node.exception_token else frozenset()
        )
        return [
            _Option(
                licenses=licenses,
                exceptions=exceptions,
                license_refs=license_refs,
                unknown=unknown,
                sources=(node.text,),
            )
        ]
    if isinstance(node, _Or):
        return _evaluate(node.left, policy) + _evaluate(node.right, policy)
    left_options = _evaluate(node.left, policy)
    right_options = _evaluate(node.right, policy)
    return [
        _Option(
            licenses=left.licenses | right.licenses,
            exceptions=left.exceptions | right.exceptions,
            license_refs=left.license_refs | right.license_refs,
            unknown=left.unknown | right.unknown,
            sources=left.sources + right.sources,
        )
        for left in left_options
        for right in right_options
    ]


def _size(node: _Node) -> int:
    if isinstance(node, _Leaf):
        return 1
    return _size(node.left) + _size(node.right)


def _find_blocker(node: _Node, policy: LicensePolicy) -> _Node:
    """The minimal sub-expression responsible for node having no options.

    Only called when ``_evaluate(node, policy)`` is empty.
    """
    if isinstance(node, _Leaf):
        return node
    if isinstance(node, _And):
        if not _evaluate(node.left, policy):
            return _find_blocker(node.left, policy)
        return _find_blocker(node.right, policy)
    # An OR only blocks when every branch blocks; report the minimal cause,
    # breaking ties towards the left-most branch.
    return min(
        (_find_blocker(node.left, policy), _find_blocker(node.right, policy)),
        key=lambda blocker: (_size(blocker), blocker.position),
    )


def _collect_blocked_by(node: _Node, policy: LicensePolicy) -> frozenset[str]:
    if isinstance(node, _Leaf):
        return _leaf_unknown(node, policy)
    return _collect_blocked_by(node.left, policy) | _collect_blocked_by(
        node.right, policy
    )


def _option_rank(
    option: _Option, preference: Iterable[str]
) -> tuple[list[int], list[str]]:
    preference_index = {id_.lower(): rank for rank, id_ in enumerate(preference)}
    identifiers = [_base_license_id(id_).lower() for id_ in sorted(option.licenses)] + [
        ref.lower() for ref in sorted(option.license_refs)
    ]
    ranks = sorted(
        preference_index.get(id_, len(preference_index)) for id_ in identifiers
    )
    return ranks, identifiers


def select_license_expression(
    raw_license_expression: str,
    policy: LicensePolicy,
) -> LicenseSelection:
    """Select an acceptable branch of a License-Expression under a policy.

    The expression is first normalized with
    :func:`canonicalize_license_expression`, then evaluated against the
    caller-provided :class:`LicensePolicy`. ``AND`` requires both sides to
    be satisfiable, ``OR`` requires one complete branch; a single allowed
    ID appearing anywhere in the expression is not sufficient.

    When several branches are equally acceptable, the policy's
    ``preference`` ordering breaks the tie.

    :param raw_license_expression: The License-Expression to evaluate.
    :param policy: The caller-provided acceptability policy.
    :raises InvalidLicenseExpression: If the expression is invalid.
    :return: A :class:`LicenseSelection` describing either the selected
        branch (with the license/exception sets it requires) or the
        minimal blocking sub-expression.

    .. doctest::

        >>> from packaging.licenses import LicensePolicy, select_license_expression
        >>> policy = LicensePolicy(
        ...     allowed_licenses={"MIT", "Apache-2.0"},
        ...     preference=("Apache-2.0", "MIT"),
        ... )
        >>> selection = select_license_expression("MIT OR Apache-2.0", policy)
        >>> selection.accepted
        True
        >>> sorted(selection.licenses)
        ['Apache-2.0']
        >>> selection.selection
        'Apache-2.0'
        >>> rejected = select_license_expression("MIT AND GPL-2.0-only", policy)
        >>> rejected.accepted
        False
        >>> rejected.blocker
        'GPL-2.0-only'
        >>> sorted(rejected.blocked_by)
        ['GPL-2.0-only']
    """
    normalized = canonicalize_license_expression(raw_license_expression)
    root = _parse_normalized_expression(normalized)
    options = _evaluate(root, policy)

    if options:
        best = min(options, key=lambda option: _option_rank(option, policy.preference))
        return LicenseSelection(
            accepted=True,
            expression=normalized,
            licenses=best.licenses,
            exceptions=best.exceptions,
            license_refs=best.license_refs,
            unknown=best.unknown,
            sources=best.sources,
            selection=" AND ".join(best.sources),
        )

    blocker = _find_blocker(root, policy)
    return LicenseSelection(
        accepted=False,
        expression=normalized,
        blocker=blocker.text,
        blocked_by=_collect_blocked_by(blocker, policy),
    )
