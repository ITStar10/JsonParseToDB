#!/usr/bin/env python3
"""DB Browser: web UI for viewing and editing a SQLite/SQLCipher file, with user accounts.

Access model
  * Anyone can request an account (pending). An admin approves or rejects it.
  * Approved users can sign in and read data, limited by their data access rules.
  * Admins can read and edit everything and manage users.
Every /api/ route requires a signed-in user unless it is listed in PUBLIC_API;
writes, /api/reload and /api/admin/* also require the admin role. The one exception is updating a
row (PATCH): regular users may update the columns an admin allowed, in rows they can see. Regular
users can never add or delete rows.
"""
import json
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from flask import (Flask, abort, g, jsonify, redirect, request, send_from_directory,
                   session)
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import ACCESS_MODES, AuthError, RateLimiter, UserStore
from config import BASE_DIR, INSTANCE_DIR, load_settings
from database import Database, DatabaseUnavailable, NotFound, RequestError
from filters import parse_column_filters

STATIC_DIR = BASE_DIR / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
PUBLIC_API = {"/api/auth/login", "/api/auth/signup", "/api/auth/logout", "/api/auth/me"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_SAMPLE = 1_000_000
# Columns regular users may edit when an admin hasn't chosen otherwise (matched case-insensitively).
DEFAULT_EDITABLE_COLUMNS = ("curUser",)
# Read-only POST endpoints that a signed-in (non-admin) user may call.
USER_POST_API = {"/api/auth/password"}


class AccessDenied(Exception):
    """Signed-in user is not allowed to see this (HTTP 403)."""


def create_app(settings=None):
    settings = settings or load_settings()
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.json.sort_keys = False
    app.config.update(
        SECRET_KEY=settings.secret_key or None,
        SESSION_COOKIE_NAME="dbbrowser_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.session_hours),
    )
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SECRET_KEY is required")
    if settings.trust_proxy:
        # Exactly one reverse proxy in front: take client IP and scheme from its headers.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    allowed_hosts = LOCAL_HOSTS | set(settings.allowed_hosts)

    def host_only(netloc):
        netloc = (netloc or "").lower()
        return netloc[:netloc.find("]") + 1] if netloc.startswith("[") else netloc.split(":")[0]
    db = Database(settings)
    users = UserStore(settings.users_db_path or INSTANCE_DIR / "users.sqlite3")
    app.extensions["db"] = db
    app.extensions["users"] = users
    login_limit = RateLimiter(limit=10, window_seconds=15 * 60)
    signup_limit = RateLimiter(limit=20, window_seconds=60 * 60)  # per IP (offices may share one)

    def is_admin():
        return g.user is not None and g.user["role"] == "admin"

    def client_ip():
        return request.remote_addr or "?"

    # ------------------------------------------------------------- request gate
    @app.before_request
    def gate():
        # Block DNS rebinding: only accept local Host headers, plus ALLOWED_HOSTS
        # (the public name when served behind a reverse proxy).
        if (db.settings.host in LOCAL_HOSTS or settings.allowed_hosts) and not app.testing:
            if host_only(request.host) not in allowed_hosts:
                abort(403)
        if request.method in WRITE_METHODS:
            # CSRF defence in depth: JSON bodies need a CORS preflight, and a
            # browser-sent Origin must be this server or an allowed host.
            origin = request.headers.get("Origin")
            if origin:
                netloc = urlsplit(origin).netloc
                if netloc != request.host and host_only(netloc) not in set(settings.allowed_hosts):
                    return jsonify(error="Cross-site request blocked"), 403
            if request.method != "DELETE" and not request.is_json:
                return jsonify(error="Expected a JSON request body"), 415

        # Load the user fresh on every request: status/role changes apply immediately.
        # The cookie holds a random session token; it must still be on record server-side
        # (signing out, admin actions and expiry remove it).
        g.user = None
        token = session.get("sid")
        if token is not None or session.get("uid") is not None:
            uid = users.session_user(token)
            row = users.get(uid) if uid is not None else None
            if row and row["status"] == "approved" and session.get("ver") == row["session_version"]:
                g.user = row
            else:
                session.clear()

        path = request.path
        if not path.startswith("/api/") or path in PUBLIC_API:
            return None
        if g.user is None:
            return jsonify(error="Please sign in.", code="auth_required"), 401
        user_row_update = request.method == "PATCH" and request.endpoint == "update_row"
        admin_only = (path.startswith("/api/admin/") or path == "/api/reload"
                      or (request.method in WRITE_METHODS and path not in USER_POST_API
                          and not user_row_update))
        if admin_only and not is_admin():
            return jsonify(error="Administrator access is required.", code="forbidden"), 403
        return None

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.path.startswith("/api/") or request.path in ("/", "/login", "/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------- errors
    @app.errorhandler(DatabaseUnavailable)
    def unavailable(exc):
        message = str(exc) if is_admin() else \
            "The database is currently unavailable. Please contact an administrator."
        return jsonify(error=message, code="database_unavailable"), 503

    @app.errorhandler(RequestError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(NotFound)
    def not_found(exc):
        return jsonify(error=str(exc)), 404

    @app.errorhandler(AccessDenied)
    def denied(exc):
        return jsonify(error=str(exc), code="forbidden"), 403

    @app.errorhandler(AuthError)
    def auth_error(exc):
        return jsonify(error=str(exc), code=exc.code), exc.status

    # ------------------------------------------------------------- pages
    def page(name):
        return send_from_directory(STATIC_DIR, name)

    @app.get("/")
    def index():
        return page("index.html") if g.user else redirect("/login")

    @app.get("/login")
    def login_page():
        return redirect("/") if g.user else page("login.html")

    @app.get("/admin")
    def admin_page():
        if not g.user:
            return redirect("/login")
        return page("admin.html") if is_admin() else redirect("/")

    # ------------------------------------------------------------- data access scope
    def scope_for(table):
        """What the current user may see in `table`: None (no access) or
        {"filters": [...], "sample": {"size", "seed"} | None}.

        The sample keeps `size` pseudo-random rows among those matching the filters. The seed is
        specific to the user, so every user gets their own stable random selection.
        Fails closed: a rule that can't be applied denies access instead of showing all rows.
        """
        if is_admin():
            return {"filters": [], "sample": None}
        if not visible_for(table):
            return None  # every column hidden (or the configured ones no longer exist)
        rule = users.rule_for(g.user["id"], table.name)  # own rule, else default, else all
        if rule is None:
            return {"filters": [], "sample": None}
        if rule["mode"] == "none":
            return None
        filters = []
        if rule["mode"] == "filtered":
            try:
                filters = parse_column_filters(table, rule["filters"], strict=True)
            except (RequestError, NotFound):
                return None
            if not filters:
                return None
        elif rule["mode"] != "all":
            return None
        sample = None
        if rule.get("sample"):
            if not table.key_expr:
                return None  # can't sample rows without a row key
            sample = {"size": int(rule["sample"]), "seed": f"{rule.get('seed') or 0}:{g.user['id']}"}
        return {"filters": filters, "sample": sample}

    def require_scope(table):
        scope = scope_for(table)
        if scope is None:
            raise AccessDenied("You don't have access to this table. Contact an administrator.")
        return scope

    def editable_candidates(table):
        """Columns that can ever be made editable for users: real, non-key, non-binary columns."""
        if table.readonly or not table.key_expr:
            return []
        return [c.name for c in table.columns
                if not c.readonly and c.kind != "blob" and c.name != table.key_column]

    def users_visible(table):
        """Columns regular users may see: an admin's list (minus columns that no longer exist),
        else all columns. Independent of who is asking."""
        names = [c.name for c in table.columns]
        explicit = users.visible_columns().get(table.name)
        return names if explicit is None else [n for n in names if n in explicit]

    def visible_for(table):
        """Columns the current user may see (admins see all)."""
        return [c.name for c in table.columns] if is_admin() else users_visible(table)

    def hide_columns(table, row, visible):
        """Strip hidden values from a row. If the row key is itself a hidden real column (not the
        internal rowid), drop the key too so the hidden value isn't sent."""
        row["values"] = {k: v for k, v in row["values"].items() if k in visible}
        if table.key_column and table.key_column not in visible and not table.column(table.key_column).readonly:
            row["key"] = None
        return row

    def check_user_columns(table, visible, filters):
        """Reject user-supplied filters on columns the user can't see (reported as unknown, so a
        hidden column's existence isn't revealed)."""
        allowed = set(visible)

        def walk(node):
            if isinstance(node, dict):
                for group in ("any", "all"):
                    if isinstance(node.get(group), list):
                        for child in node[group]:
                            walk(child)
                if "column" in node and str(node["column"]) not in allowed:
                    raise RequestError(f"Unknown column: {node['column']}")
        for f in filters:
            walk(f)

    def editable_for(table):
        """Columns regular users may edit in `table` (admins can edit everything). Always computed
        for regular users, also when an admin looks at the setting."""
        visible = set(users_visible(table))
        candidates = [c for c in editable_candidates(table) if c in visible]
        explicit = users.editable_columns().get(table.name)
        if explicit is None:
            wanted = {d.lower() for d in DEFAULT_EDITABLE_COLUMNS}
            return [c for c in candidates if c.lower() in wanted]
        return [c for c in candidates if c in explicit]  # columns renamed/removed since are dropped

    def json_arg(name, default, kind):
        try:
            value = json.loads(request.args.get(name) or default)
        except ValueError:
            raise RequestError(f"Invalid '{name}'") from None
        if not isinstance(value, kind):
            raise RequestError(f"Invalid '{name}'")
        return value

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise RequestError("Expected a JSON object")
        return data

    # ------------------------------------------------------------- auth api
    @app.get("/api/auth/me")
    def me():
        return jsonify(user=users.public(g.user), hasAdmin=users.count_admins() > 0)

    @app.post("/api/auth/signup")
    def signup():
        if not signup_limit.hit(client_ip()):
            return jsonify(error="Too many sign-up requests. Try again later."), 429
        data = body()
        users.create(data.get("fullName"), data.get("email"), data.get("password"))
        return jsonify(message="Your request was sent. You can sign in once an administrator "
                               "approves it."), 201

    @app.post("/api/auth/login")
    def login():
        data = body()
        email = data.get("email") if isinstance(data.get("email"), str) else ""
        key = (client_ip(), email.strip().lower())
        if not login_limit.hit(key):
            return jsonify(error="Too many sign-in attempts. Wait a few minutes and try again."), 429
        row = users.authenticate(email, data.get("password"))
        login_limit.reset(key)
        users.end_session(session.get("sid"))  # replace any previous session in this browser
        session.clear()
        session.permanent = True
        session["sid"] = users.create_session(row["id"], settings.session_hours)
        session["ver"] = row["session_version"]
        return jsonify(user=users.public(row))

    @app.post("/api/auth/logout")
    def logout():
        users.end_session(session.get("sid"))  # a copied cookie stops working too
        session.clear()
        return jsonify(ok=True)

    @app.post("/api/auth/password")
    def change_password():
        data = body()
        try:
            users.authenticate(g.user["email"], data.get("currentPassword"))
        except AuthError:
            raise RequestError("Your current password is incorrect") from None
        users.set_password(g.user["id"], data.get("newPassword"))
        users.end_user_sessions(g.user["id"], keep_token=session.get("sid"))  # end the others
        row = users.get(g.user["id"])
        session["ver"] = row["session_version"]  # keep this session
        return jsonify(ok=True)

    # ------------------------------------------------------------- data api
    @app.get("/api/status")
    def status():
        info = db.status()
        info["user"] = users.public(g.user)
        if not is_admin():
            info.pop("path", None)
            if not info["ok"]:
                info["error"] = "The database is currently unavailable. Please contact an administrator."
        return jsonify(info)

    @app.post("/api/reload")
    def reload():
        """Re-read .env and reopen the database (e.g. after fixing DB_PASSWORD)."""
        fresh = load_settings()
        info = db.reload(fresh)
        info["user"] = users.public(g.user)
        return jsonify(info)

    @app.get("/api/tables")
    def tables():
        return jsonify(tables=db.tables(scope=scope_for))

    def query_filters(table, except_column=None):
        """The user's own filters (filter boxes + raw filter trees), limited to visible columns."""
        column_filters = json_arg("columnFilters", "{}", dict)
        if except_column is not None:
            column_filters.pop(except_column, None)
        raw = json_arg("filters", "[]", list)
        if not is_admin():
            visible = visible_for(table)
            for name in column_filters:
                if name not in visible:
                    raise RequestError(f"Unknown column: {name}")
            check_user_columns(table, visible, raw)
        return [*parse_column_filters(table, column_filters), *raw]

    @app.get("/api/tables/<path:name>/rows")
    def rows(name):
        table = db.table(name)
        scope = require_scope(table)
        try:
            page_no = int(request.args.get("page", 1))
            page_size = int(request.args.get("pageSize", 50))
        except ValueError:
            raise RequestError("Invalid query parameters") from None
        visible = visible_for(table)
        sort = request.args.get("sort") or None
        search_column = request.args.get("searchColumn") or None
        for col in (sort, search_column):
            if col is not None and col not in visible:
                raise RequestError(f"Unknown column: {col}")
        data = db.rows(name, filters=[*scope["filters"], *query_filters(table)],
                       sample=db.sample_spec(scope),
                       search=request.args.get("search", ""),
                       search_column=search_column, search_columns=visible,
                       sort=sort,
                       direction=request.args.get("dir", "asc"),
                       page=page_no, page_size=page_size)
        if not is_admin():
            data["table"]["columns"] = [c for c in data["table"]["columns"] if c["name"] in visible]
            data["rows"] = [hide_columns(table, r, visible) for r in data["rows"]]
            data["table"]["hiddenColumns"] = len(table.columns) - len(visible)
            data["table"]["editable"] = False   # no adding or deleting rows
            data["table"]["readonly"] = True
            data["table"]["editableColumns"] = [] if db.settings.read_only else editable_for(table)
        data["table"]["restricted"] = bool(scope["filters"] or scope["sample"])
        data["table"]["sample"] = scope["sample"]["size"] if scope["sample"] else None
        return jsonify(data)

    @app.get("/api/tables/<path:name>/values")
    def values(name):
        table = db.table(name)
        scope = require_scope(table)
        column = request.args.get("column")
        if not column:
            raise RequestError("'column' is required")
        if column not in visible_for(table):
            raise RequestError(f"Unknown column: {column}")
        return jsonify(db.distinct_values(name, column, search=request.args.get("search", ""),
                                          filters=[*scope["filters"], *query_filters(table, column)],
                                          sample=db.sample_spec(scope)))

    @app.post("/api/tables/<path:name>/rows")
    def create_row(name):
        return jsonify(row=db.insert(name, body().get("values", {}))), 201

    @app.patch("/api/tables/<path:name>/rows/<key>")
    def update_row(name, key):
        values = body().get("values", {})
        if is_admin():
            return jsonify(row=db.update(name, key, values))
        # Regular user: only allowed columns, only rows inside their own data access.
        table = db.table(name)
        scope = require_scope(table)
        if not isinstance(values, dict):
            raise RequestError("'values' must be an object")
        allowed = set(editable_for(table))
        denied = [c for c in values if c not in allowed]
        if denied:
            raise AccessDenied("You can't edit: " + ", ".join(sorted(map(str, denied))) + ". "
                               + ("You may edit: " + ", ".join(sorted(allowed)) + "."
                                  if allowed else "An administrator hasn't allowed editing any column."))
        if not db.row_visible(name, key, scope["filters"], db.sample_spec(scope)):
            raise NotFound("Row not found")
        return jsonify(row=hide_columns(table, db.update(name, key, values), set(visible_for(table))))

    @app.delete("/api/tables/<path:name>/rows/<key>")
    def delete_row(name, key):
        deleted = db.delete(name, [key])
        if not deleted:
            raise NotFound("Row not found")
        return jsonify(deleted=deleted)

    @app.post("/api/tables/<path:name>/delete")
    def delete_rows(name):
        return jsonify(deleted=db.delete(name, body().get("keys")))

    # ------------------------------------------------------------- admin api
    def user_id_arg(user_id):
        users.require(user_id)
        return user_id

    @app.get("/api/admin/users")
    def admin_users():
        return jsonify(users=users.list(request.args.get("status") or None), counts=users.counts())

    @app.post("/api/admin/users/<int:user_id>/status")
    def admin_set_status(user_id):
        return jsonify(user=users.set_status(user_id_arg(user_id), body().get("status"), g.user["id"]))

    @app.post("/api/admin/users/<int:user_id>/role")
    def admin_set_role(user_id):
        return jsonify(user=users.set_role(user_id_arg(user_id), body().get("role"), g.user["id"]))

    @app.post("/api/admin/users/<int:user_id>/password")
    def admin_set_password(user_id):
        users.set_password(user_id_arg(user_id), body().get("password"))
        users.end_user_sessions(user_id)
        return jsonify(ok=True)

    @app.delete("/api/admin/users/<int:user_id>")
    def admin_delete_user(user_id):
        users.delete(user_id_arg(user_id), g.user["id"])
        return jsonify(ok=True)

    def validate_rules(rules, existing, *, allow_inherit):
        """Check admin-supplied rules. For a user, mode "inherit" means "use the default"
        and is simply not stored; the default itself can't inherit.

        A rule may carry "sample": N (show only N random rows per user). The random seed is kept
        from the existing rule so the selection stays stable, unless "reshuffle" is true.
        """
        if not isinstance(rules, dict):
            raise RequestError("'rules' must be an object")
        clean = {}
        for table_name, rule in rules.items():
            table = db.table(str(table_name))
            mode = rule.get("mode") if isinstance(rule, dict) else None
            if mode == "inherit" and allow_inherit:
                continue
            if mode not in ACCESS_MODES:
                raise RequestError(f"Invalid access mode for {table.name}")
            filters = {}
            if mode == "filtered":
                raw = rule.get("filters") or {}
                if not isinstance(raw, dict):
                    raise RequestError(f"Invalid filters for {table.name}")
                filters = {k: v for k, v in raw.items() if isinstance(v, str) and v.strip()}
                if not parse_column_filters(table, filters, strict=True):
                    raise RequestError(f"Add at least one filter for {table.name}, "
                                       "or choose 'All rows' or 'No access'.")
            sample = rule.get("sample")
            if sample in (None, "", 0):
                sample = None
            elif mode == "none":
                sample = None
            else:
                if isinstance(sample, bool) or not isinstance(sample, int) or not 1 <= sample <= MAX_SAMPLE:
                    raise RequestError(f"The random sample size for {table.name} must be a whole "
                                       f"number between 1 and {MAX_SAMPLE:,}")
                if not table.key_expr:
                    raise RequestError(f"{table.name} has no row key, so it can't be sampled")
            prev = existing.get(table.name) or {}
            keep = bool(sample) and bool(prev.get("sample")) and not rule.get("reshuffle")
            seed = prev.get("seed") or secrets.randbits(62) if keep else (secrets.randbits(62) if sample else 0)
            clean[table.name] = {"mode": mode, "filters": filters, "sample": sample, "seed": seed}
        return clean

    def tables_with_columns():
        out = []
        for t in db.tables():
            meta = db.table(t["name"]).as_dict()
            out.append({"name": t["name"], "kind": t["kind"], "rows": t["rows"],
                        "columns": [{"name": c["name"], "type": c["type"], "kind": c["kind"]}
                                    for c in meta["columns"]]})
        return out

    @app.get("/api/admin/editable-columns")
    def admin_get_editable():
        """Which columns regular users may edit, per table (admins edit everything)."""
        explicit = users.editable_columns()
        shown = users.visible_columns()
        tables = []
        for t in db.tables():
            table = db.table(t["name"])
            hidden = set() if t["name"] not in shown else {c.name for c in table.columns} - set(shown[t["name"]])
            tables.append({"name": t["name"], "kind": t["kind"],
                           "candidates": [{"name": c.name, "type": c.type, "kind": c.kind, "hidden": c.name in hidden}
                                          for c in table.columns if c.name in editable_candidates(table)],
                           "editable": editable_for(table), "isDefault": t["name"] not in explicit,
                           "locked": not editable_candidates(table)})
        return jsonify(tables=tables, defaultColumns=list(DEFAULT_EDITABLE_COLUMNS))

    @app.put("/api/admin/editable-columns")
    def admin_put_editable():
        """Replace per-table settings: {rules: {table: [columns] | null}}; null = built-in default."""
        rules = body().get("rules")
        if not isinstance(rules, dict):
            raise RequestError("'rules' must be an object")
        clean = {}
        for table_name, columns in rules.items():
            table = db.table(str(table_name))
            if columns is None:
                continue
            if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
                raise RequestError(f"Invalid column list for {table.name}")
            candidates = set(editable_candidates(table))
            bad = [c for c in columns if c not in candidates]
            if bad:
                raise RequestError(f"{table.name}: these columns can't be made editable: {', '.join(bad)}")
            clean[table.name] = list(dict.fromkeys(columns))
        users.set_editable_columns(clean)
        return admin_get_editable()

    @app.get("/api/admin/visible-columns")
    def admin_get_visible():
        """Which columns regular users can see, per table (admins always see all)."""
        explicit = users.visible_columns()
        tables = []
        for t in db.tables():
            table = db.table(t["name"])
            names = [c.name for c in table.columns]
            chosen = explicit.get(t["name"])
            tables.append({"name": t["name"], "kind": t["kind"],
                           "columns": [{"name": c.name, "type": c.type, "kind": c.kind} for c in table.columns],
                           "visible": names if chosen is None else [n for n in names if n in chosen],
                           "isDefault": chosen is None})
        return jsonify(tables=tables)

    @app.put("/api/admin/visible-columns")
    def admin_put_visible():
        """Replace per-table settings: {rules: {table: [columns] | null}}; null = all columns."""
        rules = body().get("rules")
        if not isinstance(rules, dict):
            raise RequestError("'rules' must be an object")
        clean = {}
        for table_name, columns in rules.items():
            table = db.table(str(table_name))
            if columns is None:
                continue
            if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
                raise RequestError(f"Invalid column list for {table.name}")
            names = {c.name for c in table.columns}
            bad = [c for c in columns if c not in names]
            if bad:
                raise RequestError(f"{table.name}: unknown columns: {', '.join(bad)}")
            if not columns:
                raise RequestError(f"Keep at least one column visible in {table.name}, "
                                   "or hide the whole table with 'Show to users…'.")
            clean[table.name] = list(dict.fromkeys(columns))
        users.set_visible_columns(clean)
        return admin_get_visible()

    @app.get("/api/admin/access-defaults")
    def admin_get_defaults():
        """Default data access for all regular users (they inherit it unless overridden)."""
        return jsonify(tables=tables_with_columns(), rules=users.default_rules())

    @app.put("/api/admin/access-defaults")
    def admin_put_defaults():
        users.set_default_rules(validate_rules(body().get("rules"), users.default_rules(), allow_inherit=False))
        return jsonify(rules=users.default_rules())

    @app.get("/api/admin/users/<int:user_id>/access")
    def admin_get_access(user_id):
        user = users.public(users.require(user_id))
        return jsonify(user=user, tables=tables_with_columns(), rules=users.access_rules(user_id),
                       defaults=users.default_rules())

    @app.put("/api/admin/users/<int:user_id>/access")
    def admin_put_access(user_id):
        user_id_arg(user_id)
        data = body()
        rules = validate_rules(data.get("rules"), users.access_rules(user_id), allow_inherit=True)
        users.set_access_rules(user_id, rules)
        user = users.public(users.get(user_id))
        if data.get("approve"):
            user = users.set_status(user_id, "approved", g.user["id"])
        return jsonify(user=user, rules=users.access_rules(user_id))

    @app.post("/api/admin/access-preview")
    def admin_access_preview():
        """How many rows a filter set would show, so admins can check rules before saving."""
        data = body()
        table = db.table(str(data.get("table", "")))
        filters = parse_column_filters(table, data.get("filters") or {}, strict=True)
        matched = db.rows(table.name, filters=filters, page_size=1)["total"]
        total = db.rows(table.name, page_size=1)["total"]
        return jsonify(matched=matched, total=total)

    return app


def main():
    settings = load_settings()
    app = create_app(settings)
    status = app.extensions["db"].status()
    users = app.extensions["users"]
    print(f" * Database: {status['path'] or '(none found)'}"
          f"{'  [SQLCipher]' if status['encrypted'] else ''}"
          f"{'  [read-only]' if status['readOnly'] else ''}")
    print(f" * Users:    {users.path}")
    if not status["ok"]:
        print(f" ! {status['error']}")
    if users.count_admins() == 0:
        print(" ! No administrator yet. Create one with:  python manage.py create-admin")
    app.run(host=settings.host, port=settings.port, debug=settings.debug,
            threaded=True, load_dotenv=False)  # config.py reads .env itself


if __name__ == "__main__":
    main()
