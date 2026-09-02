"""Backtracking-safe regex compilation shared by the pod-name filter and probes.

Python's ``re`` has no match timeout, so any pattern that reaches a render or
fetch thread must be screened first: an over-long term or a quantifier applied
to a group whose body already holds an unbounded quantifier (``(a+)+``,
``(.*)*``, ``(a{1,5})+``) can backtrack for minutes on a short input and freeze
that thread for good. :func:`safe_compile` returns ``None`` for such patterns
(and for invalid ones) so callers fall back to a plain substring match, or skip
the field, instead of ever running an unbounded match.

Import-light on purpose (stdlib only): used by the data layer (probes) and the
renderer alike.
"""

from __future__ import annotations

import re
from typing import Optional, Pattern

__all__ = ["REGEX_META", "REGEX_MAX_LEN", "has_nested_quantifier",
           "looks_like_regex", "safe_compile"]

# Characters that, when present in a term, make us attempt a regex match.
REGEX_META = frozenset(r".^$*+?[]{}|()\\")
# Cap a pattern's length before compiling it; together with the nested-
# quantifier guard this bounds catastrophic backtracking.
REGEX_MAX_LEN = 200


def has_nested_quantifier(pattern: str) -> bool:
    """True for the catastrophic-backtracking family — a quantifier applied to
    a group whose body already holds an unbounded quantifier."""
    stack = [False]  # per open group: does its body hold an unbounded quant?
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "(":
            stack.append(False)
        elif c == ")":
            inner = stack.pop() if len(stack) > 1 else False
            nxt = pattern[i + 1] if i + 1 < n else ""
            # tuple membership, not `nxt in "*+{"` — an empty nxt (group at
            # end of pattern) is a substring of every str and would wrongly
            # flag a safe, unquantified group like ``(a+)``.
            quantified = nxt in ("*", "+", "{")
            if inner and quantified:
                return True
            if quantified and stack:
                stack[-1] = True  # a quantified group bubbles up to its parent
        elif c in ("*", "+", "{"):
            stack[-1] = True
        i += 1
    return False


def looks_like_regex(term: str) -> bool:
    """True when ``term`` carries at least one regex metacharacter."""
    return any(ch in REGEX_META for ch in term)


def safe_compile(pattern: str, flags: int = 0, *,
                 max_len: int = REGEX_MAX_LEN) -> Optional[Pattern]:
    """Compile ``pattern`` unless it is empty, over-long, invalid, or shaped
    for catastrophic backtracking — those yield ``None`` and never raise."""
    if not pattern or len(pattern) > max_len:
        return None
    if has_nested_quantifier(pattern):
        return None
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None
