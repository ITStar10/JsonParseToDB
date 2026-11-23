"""Parse the per-column filter-box syntax into API filter trees.

This is the single source of truth for what a filter box means; the browser
sends the raw text and the server parses it (both for the user's own filters
and for the admin-defined data access filters).

Syntax for one box (terms separated by commas outside double quotes):
    austin, boston   any of several values (positive terms are OR-ed)
    =a               equals (case-insensitive)      !a   does not contain
    !=a              not equal                      > >= < <=  compare
    a..b  a..  ..b   inclusive range (partial dates cover the whole year/month)
    =                empty or NULL                  != / !   not empty
    ="x, y"          quote values containing commas
Negative terms are AND-ed. Boolean columns take "1", "0" or "null".
"""
import re
from datetime import date, timedelta

from database import RequestError

PREFIXES = ((">=", "gte"), ("<=", "lte"), ("!=", "not_equals"), (">", "gt"), ("<", "lt"),
            ("=", "equals"), ("!", "not_contains"))
NEGATIVE = {"not_equals", "not_contains", "not_empty"}
MAX_TEXT = 4000


def split_terms(text):
    out, cur, quoted, i = [], [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            if quoted and text[i + 1:i + 2] == '"':
                cur.append('""')
                i += 2
                continue
            quoted = not quoted
        elif ch == "," and not quoted:
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [t.strip() for t in out if t.strip()]


def _is_quoted(v):
    return len(v) >= 2 and v[0] == '"' and v[-1] == '"'


def unquote(v):
    v = v.strip()
    return v[1:-1].replace('""', '"') if _is_quoted(v) else v


def date_upper_bound(v):
    """Exclusive upper bound so '..1975' / '..1975-06' include the whole year / month."""
    if re.fullmatch(r"\d{4}", v):
        return str(int(v) + 1)
    m = re.fullmatch(r"(\d{4})-(\d{2})", v)
    if m:
        y, mo = int(m[1]), int(m[2])
        return f"{y + 1}-01" if mo == 12 else f"{y}-{mo + 1:02d}"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        try:
            return (date(int(m[1]), int(m[2]), int(m[3])) + timedelta(days=1)).isoformat()
        except ValueError:
            return None
    return None


def parse_term(col, term):
    column = col.name
    if term == "=":
        return {"column": column, "op": "is_empty"}
    if term in ("!=", "!"):
        return {"column": column, "op": "not_empty"}
    for prefix, op in PREFIXES:
        if term.startswith(prefix):
            value = unquote(term[len(prefix):])
            return {"column": column, "op": op, "value": value} if value != "" else None
    if not _is_quoted(term) and ".." in term:
        lo, hi = (unquote(x) for x in term.split("..", 1))
        parts = []
        if lo:
            parts.append({"column": column, "op": "gte", "value": lo})
        if hi:
            nxt = date_upper_bound(hi) if col.kind == "date" else None
            parts.append({"column": column, "op": "lt", "value": nxt} if nxt
                         else {"column": column, "op": "lte", "value": hi})
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else {"all": parts}
    return {"column": column, "op": "contains", "value": unquote(term)}


def parse_column_filter(col, raw):
    """Text from one filter box -> filter tree, or None when the box is empty."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or len(raw) > MAX_TEXT:
        raise RequestError(f"Invalid filter for {col.name}")
    if col.kind == "boolean":
        if raw == "null":
            return {"column": col.name, "op": "is_null"}
        if raw in ("0", "1"):
            return {"column": col.name, "op": "equals", "value": raw}
        raise RequestError(f"{col.name} filter must be 1, 0 or null")
    pos, neg = [], []
    for term in split_terms(raw):
        f = parse_term(col, term)
        if f:
            (neg if f.get("op") in NEGATIVE else pos).append(f)
    parts = ([{"any": pos}] if len(pos) > 1 else pos) + neg
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"all": parts}


def parse_column_filters(table, mapping, *, strict=False):
    """{column: text} -> list of filter trees (AND-ed).

    With strict=True (used for admin access rules), a non-empty box that yields
    no condition is an error instead of silently meaning "no restriction".
    """
    if mapping is None:
        return []
    if not isinstance(mapping, dict):
        raise RequestError("Column filters must be an object")
    out = []
    for name, text in mapping.items():
        col = table.column(str(name))
        f = parse_column_filter(col, text)
        if f is None and strict and isinstance(text, str) and text.strip():
            raise RequestError(f"The filter for {col.name} has no usable condition: {text!r}")
        if f:
            out.append(f)
    return out
