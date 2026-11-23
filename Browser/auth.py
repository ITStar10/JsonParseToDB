"""User accounts, sign-up approval and per-user data access rules.

Stored in a separate plain SQLite file (default Browser/instance/users.sqlite3),
so the data database is never modified for authentication purposes.
"""
import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from database import NotFound, RequestError

STATUSES = ("pending", "approved", "rejected", "disabled")
ROLES = ("admin", "user")
ACCESS_MODES = ("all", "filtered", "none")
MIN_PASSWORD = 8
MAX_PASSWORD = 200
MAX_NAME = 120
MAX_EMAIL = 254
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Used to keep login timing similar for unknown e-mail addresses.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'disabled')),
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS default_access_rules (
    table_name TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('all', 'filtered', 'none')),
    filters TEXT NOT NULL DEFAULT '{}',   -- {"column": "filter-box text"}
    sample_size INTEGER,                  -- show only this many random rows per user (NULL = all)
    seed INTEGER NOT NULL DEFAULT 0,      -- changes which random rows ("reshuffle")
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visible_columns (
    table_name TEXT PRIMARY KEY,
    columns TEXT NOT NULL,                -- JSON list of column names regular users may see
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS editable_columns (
    table_name TEXT PRIMARY KEY,
    columns TEXT NOT NULL,                -- JSON list of column names regular users may edit
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_rules (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    table_name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('all', 'filtered', 'none')),
    filters TEXT NOT NULL DEFAULT '{}',   -- {"column": "filter-box text"}
    sample_size INTEGER,
    seed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, table_name)
);
"""


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class AuthError(Exception):
    """Sign-in problem; message is safe to show. `status` is the HTTP code."""

    def __init__(self, message, status=401, code="unauthorized"):
        super().__init__(message)
        self.status = status
        self.code = code


def validate_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise RequestError(f"Password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise RequestError("Password is too long")


def normalize_email(email):
    email = (email or "").strip() if isinstance(email, str) else ""
    if not email or len(email) > MAX_EMAIL or not EMAIL_RE.match(email):
        raise RequestError("Enter a valid e-mail address")
    return email


def normalize_name(name):
    name = " ".join(name.split()) if isinstance(name, str) else ""
    if not name:
        raise RequestError("Enter your full name")
    if len(name) > MAX_NAME:
        raise RequestError("Full name is too long")
    return name


class UserStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Add columns introduced after the first release to existing users databases."""
        for table in ("access_rules", "default_access_rules"):
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if "sample_size" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN sample_size INTEGER")
            if "seed" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN seed INTEGER NOT NULL DEFAULT 0")

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ helpers
    def _one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @staticmethod
    def public(row):
        if row is None:
            return None
        return {"id": row["id"], "fullName": row["full_name"], "email": row["email"],
                "role": row["role"], "status": row["status"], "createdAt": row["created_at"],
                "decidedAt": row["decided_at"], "lastLoginAt": row["last_login_at"]}

    def get(self, user_id):
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def require(self, user_id):
        row = self.get(user_id)
        if row is None:
            raise NotFound("User not found")
        return row

    def count_admins(self, *, approved_only=True):
        sql = "SELECT count(*) FROM users WHERE role = 'admin'"
        if approved_only:
            sql += " AND status = 'approved'"
        return self._one(sql)[0]

    # ------------------------------------------------------------ accounts
    def create(self, full_name, email, password, *, role="user", status="pending"):
        full_name, email = normalize_name(full_name), normalize_email(email)
        validate_password(password)
        if role not in ROLES or status not in STATUSES:
            raise RequestError("Invalid role or status")
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (full_name, email, password_hash, role, status, created_at, "
                    "decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (full_name, email, generate_password_hash(password), role, status, now(),
                     now() if status != "pending" else None))
            except sqlite3.IntegrityError:
                raise RequestError("An account with this e-mail already exists "
                                   "(it may still be waiting for approval).") from None
            return self.get(cur.lastrowid)

    def authenticate(self, email, password):
        if not isinstance(email, str) or not isinstance(password, str) or len(password) > MAX_PASSWORD:
            raise AuthError("Incorrect e-mail or password")
        row = self._one("SELECT * FROM users WHERE email = ?", (email.strip(),))
        if row is None:
            check_password_hash(_DUMMY_HASH, password)
            raise AuthError("Incorrect e-mail or password")
        if not check_password_hash(row["password_hash"], password):
            raise AuthError("Incorrect e-mail or password")
        if row["status"] == "pending":
            raise AuthError("Your sign-up request is waiting for an administrator to approve it.",
                            403, "pending")
        if row["status"] == "rejected":
            raise AuthError("Your sign-up request was not approved.", 403, "rejected")
        if row["status"] == "disabled":
            raise AuthError("Your account is disabled. Contact an administrator.", 403, "disabled")
        with self._lock:
            self._conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), row["id"]))
        return self.get(row["id"])

    def list(self, status=None):
        sql = "SELECT * FROM users"
        params = ()
        if status:
            if status not in STATUSES:
                raise RequestError("Unknown status")
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC, id DESC"
        with self._lock:
            return [self.public(r) for r in self._conn.execute(sql, params)]

    def counts(self):
        with self._lock:
            rows = self._conn.execute("SELECT status, count(*) FROM users GROUP BY status").fetchall()
        out = dict.fromkeys(STATUSES, 0)
        out.update({s: n for s, n in rows})
        return out

    def _guard_last_admin(self, target, actor_id, action):
        if target["id"] == actor_id:
            raise RequestError(f"You can't {action} your own account")
        if target["role"] == "admin" and target["status"] == "approved" and self.count_admins() <= 1:
            raise RequestError(f"You can't {action} the last administrator")

    def set_status(self, user_id, status, actor_id):
        if status not in STATUSES or status == "pending":
            raise RequestError("Invalid status")
        with self._lock:
            target = self.require(user_id)
            if status != "approved":
                self._guard_last_admin(target, actor_id, "disable" if status == "disabled" else "reject")
            # Bump the session version so existing sessions end immediately.
            self._conn.execute(
                "UPDATE users SET status = ?, decided_at = ?, decided_by = ?, "
                "session_version = session_version + 1 WHERE id = ?",
                (status, now(), actor_id, user_id))
            return self.public(self.get(user_id))

    def set_role(self, user_id, role, actor_id):
        if role not in ROLES:
            raise RequestError("Invalid role")
        with self._lock:
            target = self.require(user_id)
            if role != "admin":
                self._guard_last_admin(target, actor_id, "demote")
            self._conn.execute("UPDATE users SET role = ?, session_version = session_version + 1 "
                               "WHERE id = ?", (role, user_id))
            return self.public(self.get(user_id))

    def set_password(self, user_id, password):
        validate_password(password)
        with self._lock:
            self.require(user_id)
            self._conn.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 "
                               "WHERE id = ?", (generate_password_hash(password), user_id))

    def delete(self, user_id, actor_id):
        with self._lock:
            target = self.require(user_id)
            self._guard_last_admin(target, actor_id, "delete")
            self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # ------------------------------------------------------------ data access rules
    # Precedence per table: the user's own rule, else the default rule, else all rows.
    # A user rule row always exists only for an explicit choice (including "all");
    # no row means "use the default".
    @staticmethod
    def _rules_from(rows):
        return {r["table_name"]: {"mode": r["mode"], "filters": json.loads(r["filters"] or "{}"),
                                  "sample": r["sample_size"], "seed": r["seed"]} for r in rows}

    def access_rules(self, user_id):
        """{table: {"mode": ..., "filters": {column: text}}} explicitly set for this user."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT table_name, mode, filters, sample_size, seed FROM access_rules WHERE user_id = ?", (user_id,)).fetchall()
        return self._rules_from(rows)

    def default_rules(self):
        """Rules applied to every regular user without a rule of their own for a table."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT table_name, mode, filters, sample_size, seed FROM default_access_rules").fetchall()
        return self._rules_from(rows)

    def rule_for(self, user_id, table_name):
        """The effective rule for one table, or None meaning 'all rows'."""
        with self._lock:
            row = self._conn.execute(
                "SELECT mode, filters, sample_size, seed FROM access_rules WHERE user_id = ? AND table_name = ?",
                (user_id, table_name)).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT mode, filters, sample_size, seed FROM default_access_rules WHERE table_name = ?",
                    (table_name,)).fetchone()
        return None if row is None else {"mode": row["mode"], "filters": json.loads(row["filters"] or "{}"),
                                         "sample": row["sample_size"], "seed": row["seed"]}

    def _replace_rules(self, delete_sql, delete_params, insert_sql, rules, extra=()):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(delete_sql, delete_params)
                for table, rule in rules.items():
                    self._conn.execute(insert_sql, (
                        *extra, table, rule["mode"], json.dumps(rule.get("filters") or {}),
                        rule.get("sample") or None, rule.get("seed") or 0, now()))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def set_access_rules(self, user_id, rules):
        """Replace this user's explicit rules. `rules` must already be validated and must
        only contain explicit choices; tables left out use the default."""
        with self._lock:
            self.require(user_id)
            self._replace_rules(
                "DELETE FROM access_rules WHERE user_id = ?", (user_id,),
                "INSERT INTO access_rules (user_id, table_name, mode, filters, sample_size, seed, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rules, extra=(user_id,))

    def set_default_rules(self, rules):
        """Replace the default rules (already validated). Tables left out mean 'all rows'."""
        self._replace_rules(
            "DELETE FROM default_access_rules", (),
            "INSERT INTO default_access_rules (table_name, mode, filters, sample_size, seed, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", rules)


    # ------------------------------------------------------------ per-table column lists
    # editable_columns: columns regular users may update (no row = built-in default, see app.py).
    # visible_columns: columns regular users may see (no row = all columns).
    _COLUMN_LISTS = {"editable": "editable_columns", "visible": "visible_columns"}

    def _column_lists(self, kind):
        with self._lock:
            rows = self._conn.execute(f"SELECT table_name, columns FROM {self._COLUMN_LISTS[kind]}").fetchall()
        return {r["table_name"]: json.loads(r["columns"]) for r in rows}

    def _set_column_lists(self, kind, rules):
        table = self._COLUMN_LISTS[kind]
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(f"DELETE FROM {table}")
                for name, columns in rules.items():
                    self._conn.execute(f"INSERT INTO {table} (table_name, columns, updated_at) VALUES (?, ?, ?)",
                                       (name, json.dumps(columns), now()))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def editable_columns(self):
        """{table: [column, ...]} explicitly set by an admin."""
        return self._column_lists("editable")

    def set_editable_columns(self, rules):
        """Replace all explicit settings. `rules` = {table: [columns]} (already validated)."""
        self._set_column_lists("editable", rules)

    def visible_columns(self):
        """{table: [column, ...]} regular users may see, where an admin restricted it."""
        return self._column_lists("visible")

    def set_visible_columns(self, rules):
        """Replace all explicit settings. `rules` = {table: [columns]} (already validated)."""
        self._set_column_lists("visible", rules)


class RateLimiter:
    """Small in-memory limiter for sign-in and sign-up attempts."""

    def __init__(self, limit, window_seconds):
        self.limit, self.window = limit, window_seconds
        self._hits = {}
        self._lock = threading.Lock()

    def hit(self, key):
        """Record an attempt; return False when the key is over the limit."""
        t = time.monotonic()
        with self._lock:
            hits = [h for h in self._hits.get(key, []) if t - h < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(t)
            self._hits[key] = hits
            if len(self._hits) > 10000:  # keep memory bounded
                self._hits = {k: v for k, v in self._hits.items() if v and t - v[-1] < self.window}
            return True

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)
