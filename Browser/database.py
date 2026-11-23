"""Access layer for a plain SQLite or SQLCipher-encrypted database file.

The browser edits the configured file in place. Table and column names come
from the database schema and are allow-listed before they are placed in SQL;
every value is passed as a bound parameter.
"""
import hashlib
import importlib
import re
import sqlite3
import threading
from dataclasses import dataclass, field

SQLITE_HEADER = b"SQLite format 3\x00"
MAX_PAGE_SIZE = 500
MAX_FILTER_NODES = 400
DISTINCT_LIMIT = 300
DATE_SAMPLE = 200
DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]*"  # ISO 8601 date / datetime
LIKE_ESCAPE = "\\"

FILTER_OPS = {
    # op: (needs_value, sql template using {c} for the column, value transform)
    "contains":     (True,  "CAST({c} AS TEXT) LIKE ? ESCAPE '\\'", "contains"),
    "not_contains": (True,  "({c} IS NULL OR CAST({c} AS TEXT) NOT LIKE ? ESCAPE '\\')", "contains"),
    "equals":       (True,  "{c} = ? COLLATE NOCASE", None),
    "not_equals":   (True,  "({c} IS NULL OR {c} <> ? COLLATE NOCASE)", None),
    "starts":       (True,  "CAST({c} AS TEXT) LIKE ? ESCAPE '\\'", "starts"),
    "ends":         (True,  "CAST({c} AS TEXT) LIKE ? ESCAPE '\\'", "ends"),
    "gt":           (True,  "{c} > ?", None),
    "gte":          (True,  "{c} >= ?", None),
    "lt":           (True,  "{c} < ?", None),
    "lte":          (True,  "{c} <= ?", None),
    "is_empty":     (False, "({c} IS NULL OR {c} = '')", None),
    "not_empty":    (False, "({c} IS NOT NULL AND {c} <> '')", None),
    "is_null":      (False, "{c} IS NULL", None),
    "not_null":     (False, "{c} IS NOT NULL", None),
}


def sample_rank(key, seed):
    """Stable pseudo-random rank of a row for a given seed (registered in SQLite as dbb_rank).

    The same (key, seed) always gives the same rank, so a user's random sample stays the same
    between requests and across pages; a different seed gives an unrelated ordering.
    """
    digest = hashlib.blake2b(f"{seed}\x00{key}".encode("utf-8", "replace"), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1  # fits SQLite's signed 64-bit integer


class DatabaseUnavailable(Exception):
    """The database could not be opened; the message is safe to show users."""


class RequestError(ValueError):
    """Invalid input from the client (HTTP 400)."""


class NotFound(LookupError):
    """Unknown table or row (HTTP 404)."""


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def escape_like(text):
    return (text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
                .replace("%", LIKE_ESCAPE + "%")
                .replace("_", LIKE_ESCAPE + "_"))


def column_kind(declared):
    t = (declared or "").upper()
    if "BOOL" in t:
        return "boolean"
    if "INT" in t:
        return "integer"
    if any(k in t for k in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "number"
    if "BLOB" in t:
        return "blob"
    if "DATE" in t or "TIME" in t:
        return "date"
    return "text"


def to_json_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$blob": len(value)}
    return value


@dataclass
class Column:
    name: str
    type: str
    kind: str
    notnull: bool
    pk: bool
    default: str | None
    readonly: bool = False

    def as_dict(self):
        return {"name": self.name, "type": self.type, "kind": self.kind,
                "notnull": self.notnull, "pk": self.pk, "default": self.default,
                "readonly": self.readonly}


@dataclass
class Table:
    name: str
    kind: str                     # "table" or "view"
    columns: list[Column] = field(default_factory=list)
    key_expr: str | None = None   # SQL expression identifying a row
    key_column: str | None = None  # column name when the key is a real column
    key_is_integer: bool = True
    readonly: bool = False

    def column(self, name):
        for col in self.columns:
            if col.name == name:
                return col
        raise RequestError(f"Unknown column: {name}")

    def as_dict(self):
        return {"name": self.name, "kind": self.kind, "readonly": self.readonly,
                "key": self.key_column, "editable": not self.readonly,
                "columns": [c.as_dict() for c in self.columns]}


class Database:
    def __init__(self, settings):
        self.settings = settings
        self._conn = None
        self._error = None
        self._driver = None
        self._lock = threading.RLock()
        self._date_cache = {}  # (table, column) -> bool

    # ------------------------------------------------------------------ connection
    @property
    def path(self):
        return self.settings.db_path

    @property
    def encrypted(self):
        return bool(self.settings.db_password)

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def reload(self, settings):
        with self._lock:
            self.close()
            self.settings = settings
            self._error = None
            self._date_cache = {}
        return self.status()

    def _driver_for_password(self):
        if not self.settings.db_password:
            return sqlite3
        if "\x00" in self.settings.db_password:
            raise DatabaseUnavailable("DB_PASSWORD cannot contain NUL characters.")
        try:
            return importlib.import_module("sqlcipher3.dbapi2")
        except (ImportError, OSError):
            raise DatabaseUnavailable(
                "DB_PASSWORD is set, but the SQLCipher driver is not installed. "
                "Run: python -m pip install -r requirements.txt") from None

    def _open(self):
        path = self.settings.db_path
        if path is None:
            raise DatabaseUnavailable(
                "No database file found. Put a .sqlite, .sqlite3 or .db file in the "
                "Browser folder (or Browser/data), or set DB_PATH in .env.")
        if not path.is_file():
            raise DatabaseUnavailable(f"Database file not found: {path}")
        with path.open("rb") as fh:
            header = fh.read(16)
        plain = header == SQLITE_HEADER or header == b""
        password = self.settings.db_password
        if password and plain:
            raise DatabaseUnavailable(
                f"DB_PASSWORD is set in .env, but {path.name} is not encrypted. "
                "Remove DB_PASSWORD (or leave it empty) to open it.")
        if not password and not plain:
            raise DatabaseUnavailable(
                f"{path.name} appears to be encrypted (or is not a SQLite file). "
                "Set DB_PASSWORD in .env to open it.")
        driver = self._driver_for_password()
        conn = driver.connect(str(path), isolation_level=None, check_same_thread=False)
        try:
            if password:
                if not conn.execute("PRAGMA cipher_version").fetchone():
                    raise DatabaseUnavailable(
                        "The installed driver does not support SQLCipher encryption.")
                # PRAGMA cannot take bound parameters; escape the SQL string literal
                # exactly like Parser/dbcreator.py does.
                conn.execute("PRAGMA key = '" + password.replace("'", "''") + "'")
            try:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except driver.DatabaseError:
                if password:
                    raise DatabaseUnavailable(
                        f"Could not decrypt {path.name}: DB_PASSWORD in .env is wrong, "
                        "or the file is not a SQLCipher database.") from None
                raise DatabaseUnavailable(
                    f"{path.name} is not a readable SQLite database.") from None
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.create_function("dbb_rank", 2, sample_rank, deterministic=True)
            if self.settings.read_only:
                conn.execute("PRAGMA query_only = ON")
        except BaseException:
            conn.close()
            raise
        self._driver = driver
        return conn

    def connection(self):
        """Return the shared connection; caller must hold self._lock."""
        if self._conn is None:
            try:
                self._conn = self._open()
                self._error = None
            except DatabaseUnavailable as exc:
                self._error = str(exc)
                raise
            except Exception as exc:  # driver/OS errors while opening
                self._error = f"Could not open database: {exc}"
                raise DatabaseUnavailable(self._error) from None
        return self._conn

    def status(self):
        with self._lock:
            info = {
                "file": self.path.name if self.path else None,
                "path": str(self.path) if self.path else None,
                "encrypted": self.encrypted,
                "readOnly": self.settings.read_only,
            }
            try:
                self.connection()
                info.update(ok=True, error=None,
                            sizeBytes=self.path.stat().st_size)
            except DatabaseUnavailable as exc:
                info.update(ok=False, error=str(exc))
            return info

    # ------------------------------------------------------------------ schema
    def tables(self, scope=None):
        """List tables/views with row counts.

        `scope(table)` may return None to hide a table, or {"filters": [...], "sample": {...}|None}
        restricting which rows are counted (per-user data access).
        """
        with self._lock:
            conn = self.connection()
            rows = conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name").fetchall()
            result = []
            for name, kind in rows:
                table = self.table(name)
                scoped = scope(table) if scope else {"filters": [], "sample": None}
                if scoped is None:
                    continue
                where, params = self._where(table, "", None, scoped["filters"],
                                            sample=self.sample_spec(scoped))
                try:
                    count = conn.execute(f"SELECT count(*) FROM {quote(name)}{where}",
                                         params).fetchone()[0]
                except self._driver.DatabaseError:
                    count = None
                result.append({"name": name, "kind": kind, "rows": count,
                               "readonly": table.readonly})
            return result

    def table(self, name):
        with self._lock:
            conn = self.connection()
            row = conn.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ? AND type IN ('table','view') "
                "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'", (name,)).fetchone()
            if row is None:
                raise NotFound(f"Unknown table: {name}")
            table = Table(name=name, kind=row[0])
            without_rowid = bool(re.search(r"\bWITHOUT\s+ROWID\b", row[1] or "", re.I))
            for _cid, cname, ctype, notnull, default, pk, hidden in conn.execute(
                    f"PRAGMA table_xinfo({quote(name)})"):
                if hidden == 1:  # hidden virtual-table column
                    continue
                table.columns.append(Column(
                    name=cname, type=ctype or "", kind=column_kind(ctype),
                    notnull=bool(notnull), pk=bool(pk), default=default,
                    readonly=hidden in (2, 3)))  # generated columns
            self._detect_dates(table)
            if table.kind == "view" or self.settings.read_only:
                table.readonly = True
                if table.kind == "view":
                    return table
            pks = [c for c in table.columns if c.pk]
            names = {c.name.lower() for c in table.columns}
            if len(pks) == 1 and pks[0].type.upper() == "INTEGER" and not without_rowid:
                # INTEGER PRIMARY KEY is an alias for rowid (e.g. Parser's "_id").
                table.key_expr, table.key_column = quote(pks[0].name), pks[0].name
                pks[0].readonly = True
                return table
            if not without_rowid:
                for alias in ("rowid", "_rowid_", "oid"):
                    if alias not in names:
                        table.key_expr = alias
                        return table
            if len(pks) == 1:
                table.key_expr, table.key_column = quote(pks[0].name), pks[0].name
                table.key_is_integer = pks[0].kind == "integer"
            else:
                table.readonly = True  # no single-value row identity
            return table

    def _detect_dates(self, table):
        """TEXT columns whose sampled values are all ISO dates are treated as dates."""
        conn = self.connection()
        for col in table.columns:
            if col.kind != "text":
                continue
            key = (table.name, col.name)
            if key not in self._date_cache:
                c = quote(col.name)
                try:
                    total, dates = conn.execute(
                        f"SELECT count(*), sum(v GLOB ?) FROM (SELECT CAST({c} AS TEXT) AS v "
                        f"FROM {quote(table.name)} WHERE {c} IS NOT NULL AND {c} <> '' LIMIT ?)",
                        (DATE_GLOB, DATE_SAMPLE)).fetchone()
                    self._date_cache[key] = bool(total) and total == dates
                except self._driver.DatabaseError:
                    self._date_cache[key] = False
            if self._date_cache[key]:
                col.kind = "date"

    # ------------------------------------------------------------------ reads
    def _row_dict(self, table, row):
        if table.key_expr:
            key, values = row[0], row[1:]
        else:
            key, values = None, row
        return {"key": key, "values": {c.name: to_json_value(v)
                                       for c, v in zip(table.columns, values)}}

    def _select_list(self, table):
        cols = ", ".join(quote(c.name) for c in table.columns)
        return f"{table.key_expr}, {cols}" if table.key_expr else cols

    @staticmethod
    def sample_spec(scoped):
        """Turn a scope ({"filters", "sample"}) into the `sample=` argument of the query methods."""
        sample = scoped.get("sample")
        return {**sample, "filters": scoped["filters"]} if sample else None

    def _where(self, table, search, search_column, filters, sample=None, search_columns=None):
        """WHERE clause. `sample` = {"size", "seed", "filters"} keeps only `size` pseudo-randomly
        chosen rows among those matching `filters` (the access rule), before other filters apply."""
        clauses, params = [], []
        if sample:
            if not table.key_expr:
                raise RequestError(f"{table.name} has no row key, so it can't be sampled")
            key = table.key_expr
            sub_where, sub_params = self._where(table, "", None, sample.get("filters"))
            clauses.append(f"{key} IN (SELECT {key} FROM {quote(table.name)}{sub_where} "
                           "ORDER BY dbb_rank(" + key + ", ?) LIMIT ?)")
            params.extend([*sub_params, str(sample["seed"]), int(sample["size"])])
        if search:
            cols = [table.column(search_column)] if search_column else [
                c for c in table.columns if c.kind != "blob"
                and (search_columns is None or c.name in search_columns)]
            if not cols:
                return " WHERE 0", []
            for term in search.split():
                like = f"%{escape_like(term)}%"
                ors = [f"CAST({quote(c.name)} AS TEXT) LIKE ? ESCAPE '\\'" for c in cols]
                clauses.append("(" + " OR ".join(ors) + ")")
                params.extend([like] * len(cols))
        budget = [MAX_FILTER_NODES]
        for flt in filters or []:
            sql = self._filter_sql(table, flt, params, budget)
            if sql:
                clauses.append(sql)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _filter_sql(self, table, flt, params, budget):
        """One filter: {column, op, value}, or a group {"any": [...]} / {"all": [...]}."""
        budget[0] -= 1
        if budget[0] < 0:
            raise RequestError("Too many filter conditions")
        if not isinstance(flt, dict):
            raise RequestError("Each filter must be an object")
        for group, joiner in (("any", " OR "), ("all", " AND ")):
            if group in flt:
                items = flt[group]
                if not isinstance(items, list):
                    raise RequestError(f"'{group}' must be a list")
                parts = [p for p in (self._filter_sql(table, i, params, budget) for i in items) if p]
                if not parts:
                    return ""
                return "(" + joiner.join(parts) + ")"
        col = table.column(str(flt.get("column", "")))
        op = flt.get("op")
        if op not in FILTER_OPS:
            raise RequestError(f"Unknown filter operator: {op}")
        needs_value, template, transform = FILTER_OPS[op]
        if needs_value:
            value = flt.get("value")
            if value is None or isinstance(value, (dict, list)):
                raise RequestError(f"Filter '{op}' on {col.name} needs a value")
            value = str(value)
            if transform == "contains":
                value = f"%{escape_like(value)}%"
            elif transform == "starts":
                value = f"{escape_like(value)}%"
            elif transform == "ends":
                value = f"%{escape_like(value)}"
            params.append(value)
        return template.format(c=quote(col.name))

    def distinct_values(self, name, column, *, search="", filters=None, sample=None):
        """Distinct values of one column (NULL and '' merged) with row counts."""
        with self._lock:
            table = self.table(name)
            col = table.column(column)
            if col.kind == "blob":
                raise RequestError("Binary columns have no value list")
            c = quote(col.name)
            where, params = self._where(table, "", None, filters, sample=sample)
            if search:
                where += (" AND " if where else " WHERE ") + \
                    f"CAST({c} AS TEXT) LIKE ? ESCAPE '\\'"
                params.append(f"%{escape_like(search)}%")
            rows = self.connection().execute(
                # Grouped case-insensitively, matching the "=value" filter (COLLATE NOCASE).
                f"SELECT min(v), count(*) FROM (SELECT CASE WHEN {c} IS NULL OR {c} = '' THEN NULL "
                f"ELSE {c} END AS v FROM {quote(name)}{where}) GROUP BY v COLLATE NOCASE "
                f"ORDER BY min(v) IS NOT NULL, min(v) COLLATE NOCASE LIMIT ?",
                [*params, DISTINCT_LIMIT + 1]).fetchall()
            more = len(rows) > DISTINCT_LIMIT
            return {"column": col.name, "more": more,
                    "values": [{"value": to_json_value(v), "count": n}
                               for v, n in rows[:DISTINCT_LIMIT]]}

    def rows(self, name, *, search="", search_column=None, filters=None,
             sort=None, direction="asc", page=1, page_size=50, sample=None, search_columns=None):
        page = max(1, int(page))
        page_size = min(MAX_PAGE_SIZE, max(1, int(page_size)))
        direction = "DESC" if str(direction).lower() == "desc" else "ASC"
        with self._lock:
            table = self.table(name)
            where, params = self._where(table, search.strip(), search_column, filters, sample=sample,
                                        search_columns=search_columns)
            order = []
            if sort:
                order.append(f"{quote(table.column(sort).name)} COLLATE NOCASE {direction}")
            if table.key_expr:
                # Stable pagination: break ties by row identity.
                order.append(f"{table.key_expr} {direction if sort else 'ASC'}")
            order_sql = (" ORDER BY " + ", ".join(order)) if order else ""
            conn = self.connection()
            total = conn.execute(
                f"SELECT count(*) FROM {quote(name)}{where}", params).fetchone()[0]
            pages = max(1, -(-total // page_size))
            page = min(page, pages)
            data = conn.execute(
                f"SELECT {self._select_list(table)} FROM {quote(name)}{where}{order_sql} "
                "LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size]).fetchall()
            return {"table": table.as_dict(), "total": total, "page": page,
                    "pageSize": page_size, "pages": pages,
                    "rows": [self._row_dict(table, r) for r in data]}

    def _fetch_one(self, table, key):
        row = self.connection().execute(
            f"SELECT {self._select_list(table)} FROM {quote(table.name)} "
            f"WHERE {table.key_expr} = ?", (key,)).fetchone()
        if row is None:
            raise NotFound("Row not found")
        return self._row_dict(table, row)

    # ------------------------------------------------------------------ writes
    def _writable(self, name):
        table = self.table(name)
        if self.settings.read_only:
            raise RequestError("The browser is running in read-only mode (READ_ONLY in .env)")
        if table.readonly or not table.key_expr:
            raise RequestError(f"{name} is read-only in this browser")
        return table

    def _key(self, table, key):
        if table.key_is_integer:
            try:
                return int(key)
            except (TypeError, ValueError):
                raise RequestError(f"Invalid row key: {key!r}") from None
        return str(key)

    @staticmethod
    def _sql_value(col, value):
        if value is None:
            return None
        if col.kind == "boolean":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int) and value in (0, 1):
                return value
            if isinstance(value, str) and value.strip().lower() in ("true", "1", "false", "0"):
                return 1 if value.strip().lower() in ("true", "1") else 0
            raise RequestError(f"{col.name} must be true, false or null")
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float, str)):
            return value  # column affinity converts numeric strings where declared
        raise RequestError(f"Unsupported value for {col.name}")

    def _assignments(self, table, values):
        if not isinstance(values, dict):
            raise RequestError("'values' must be an object")
        out = []
        for name, value in values.items():
            col = table.column(name)
            if col.readonly or col.kind == "blob":
                raise RequestError(f"{col.name} cannot be edited")
            out.append((col, self._sql_value(col, value)))
        return out

    def _run_write(self, sql, params):
        try:
            return self.connection().execute(sql, params)
        except self._driver.IntegrityError as exc:
            raise RequestError(f"Constraint failed: {exc}") from None
        except self._driver.OperationalError as exc:
            raise RequestError(f"Write failed: {exc}") from None

    def insert(self, name, values):
        with self._lock:
            table = self._writable(name)
            pairs = self._assignments(table, values)
            if pairs:
                cols = ", ".join(quote(c.name) for c, _ in pairs)
                marks = ", ".join("?" for _ in pairs)
                cur = self._run_write(
                    f"INSERT INTO {quote(name)} ({cols}) VALUES ({marks})",
                    [v for _, v in pairs])
            else:
                cur = self._run_write(f"INSERT INTO {quote(name)} DEFAULT VALUES", [])
            if table.key_column is None or table.column(table.key_column).readonly:
                key = cur.lastrowid  # rowid, or an INTEGER PRIMARY KEY alias of it
            else:
                key = dict((c.name, v) for c, v in pairs).get(table.key_column)
            return self._fetch_one(table, key)

    def row_visible(self, name, key, filters=None, sample=None):
        """True if the row exists and matches `filters` / `sample` (a user's data access)."""
        with self._lock:
            table = self.table(name)
            if not table.key_expr:
                return False
            where, params = self._where(table, "", None, filters, sample=sample)
            where += (" AND " if where else " WHERE ") + f"{table.key_expr} = ?"
            row = self.connection().execute(
                f"SELECT 1 FROM {quote(name)}{where} LIMIT 1", [*params, self._key(table, key)]).fetchone()
            return row is not None

    def update(self, name, key, values):
        with self._lock:
            table = self._writable(name)
            key = self._key(table, key)
            pairs = self._assignments(table, values)
            if not pairs:
                return self._fetch_one(table, key)
            sets = ", ".join(f"{quote(c.name)} = ?" for c, _ in pairs)
            cur = self._run_write(
                f"UPDATE {quote(name)} SET {sets} WHERE {table.key_expr} = ?",
                [*(v for _, v in pairs), key])
            if cur.rowcount == 0:
                raise NotFound("Row not found")
            if table.key_column and table.key_column in values:
                key = self._key(table, values[table.key_column])
            return self._fetch_one(table, key)

    def delete(self, name, keys):
        if not isinstance(keys, list) or not keys:
            raise RequestError("'keys' must be a non-empty list")
        with self._lock:
            table = self._writable(name)
            keys = [self._key(table, k) for k in keys]
            conn = self.connection()
            deleted = 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                for i in range(0, len(keys), 500):
                    chunk = keys[i:i + 500]
                    marks = ", ".join("?" for _ in chunk)
                    deleted += conn.execute(
                        f"DELETE FROM {quote(name)} WHERE {table.key_expr} IN ({marks})",
                        chunk).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return deleted
