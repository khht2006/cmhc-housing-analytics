"""
Split a .sql file into individual statements.

Why this is needed: DuckDB leaks named WINDOW definitions across statements
submitted in one execute() call, so two views that both declare `WINDOW w12 AS
(...)` fail with 'window "w12" is already defined' even though each compiles
fine alone. Executing statements one at a time avoids that, and gives a usable
error message naming the failing statement instead of a parser error against a
600-line blob.

The splitter tracks string literals, quoted identifiers, line comments and block
comments, so a semicolon inside any of those does not split a statement.
"""

from __future__ import annotations


def split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    in_single = in_double = in_line_comment = in_block_comment = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_single:
            buf.append(ch)
            # '' is an escaped quote inside a string literal.
            if ch == "'" and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        # Not inside anything: look for openers and the statement terminator.
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            statements.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf)
    if tail.strip():
        statements.append(tail)

    return [s for s in statements if _is_executable(s)]


def _is_executable(statement: str) -> bool:
    """True if the fragment contains SQL, not just comments and whitespace."""
    stripped = statement.strip()
    if not stripped:
        return False

    # Strip comments to see whether anything is left.
    out, i, n = [], 0, len(stripped)
    in_line = in_block = False
    while i < n:
        ch = stripped[i]
        nxt = stripped[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        out.append(ch)
        i += 1

    return bool("".join(out).strip())
