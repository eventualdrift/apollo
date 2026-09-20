r"""The fence escaping rule (spec B.4, ADR-0006).

A fence is worthless if content can forge one. Every block body placed into a
delimited region is escaped before fencing:

    encode:  \  ->  \\        <  ->  \<        >  ->  \>
    decode:  \\ ->  \         \<  ->  <        \>  ->  >

Because no `<` or `>` survives unescaped, no body can contain `<<<` or `>>>`,
so content cannot terminate its own block, fabricate a notice, or fabricate a
policy block. The transform is deterministic, total and exactly reversible.

The database always stores the unescaped original; escaping exists only in the
rendered request.
"""

from __future__ import annotations

from apollo.errors import ApolloError

FENCE_OPEN = "<<<"
FENCE_CLOSE = ">>>"


class UnescapeError(ApolloError):
    """Raised on a backslash escape that the encoder could not have produced."""


def escape(text: str) -> str:
    """Backslash first, then the angle brackets. Order matters for reversibility."""
    return text.replace("\\", "\\\\").replace("<", "\\<").replace(">", "\\>")


def unescape(text: str) -> str:
    """Left to right, so an escaped backslash cannot consume the next character."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            raise UnescapeError("trailing backslash")
        nxt = text[i + 1]
        if nxt not in ("\\", "<", ">"):
            raise UnescapeError(f"unknown escape \\{nxt}")
        out.append(nxt)
        i += 2
    return "".join(out)


def contains_fence_delimiter(text: str) -> bool:
    return FENCE_OPEN in text or FENCE_CLOSE in text


def fence(header: str, body: str, *, label: str) -> str:
    """Wrap an escaped body in a Core-generated fence.

    `header` is Core-authored metadata and is not escaped; `body` always is.
    """
    if contains_fence_delimiter(header):
        raise UnescapeError("fence header may not contain a delimiter")
    closing = f"{FENCE_OPEN}END {label}{FENCE_CLOSE}"
    return f"{FENCE_OPEN}{header}{FENCE_CLOSE}\n{escape(body)}\n{closing}"
