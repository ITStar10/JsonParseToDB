#!/usr/bin/env python3
"""DB Browser: a small local web UI for viewing and editing a SQLite/SQLCipher file."""
import json

from flask import Flask, abort, jsonify, request, send_from_directory

from config import BASE_DIR, load_settings
from database import Database, DatabaseUnavailable, NotFound, RequestError

STATIC_DIR = BASE_DIR / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def create_app(settings=None):
    settings = settings or load_settings()
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["JSON_SORT_KEYS"] = False
    app.json.sort_keys = False
    db = Database(settings)
    app.extensions["db"] = db

    # ------------------------------------------------------------- hardening
    @app.before_request
    def check_host():
        # Block DNS-rebinding when served on loopback: only accept local Host headers.
        if db.settings.host in LOCAL_HOSTS and not app.testing:
            host = request.host or ""
            host = host[:host.find("]") + 1] if host.startswith("[") else host.split(":")[0]
            if host not in LOCAL_HOSTS:
                abort(403)
        if request.method in ("POST", "PATCH", "PUT") and not request.is_json:
            return jsonify(error="Expected a JSON request body"), 415

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(DatabaseUnavailable)
    def unavailable(exc):
        return jsonify(error=str(exc), code="database_unavailable"), 503

    @app.errorhandler(RequestError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(NotFound)
    def not_found(exc):
        return jsonify(error=str(exc)), 404

    # ------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    # ------------------------------------------------------------- api
    @app.get("/api/status")
    def status():
        return jsonify(db.status())

    @app.post("/api/reload")
    def reload():
        """Re-read .env and reopen the database (e.g. after fixing DB_PASSWORD)."""
        return jsonify(db.reload(load_settings()))

    @app.get("/api/tables")
    def tables():
        return jsonify(tables=db.tables())

    @app.get("/api/tables/<path:name>/rows")
    def rows(name):
        args = request.args
        try:
            filters = json.loads(args.get("filters") or "[]")
            page = int(args.get("page", 1))
            page_size = int(args.get("pageSize", 50))
        except (ValueError, TypeError):
            raise RequestError("Invalid query parameters") from None
        if not isinstance(filters, list):
            raise RequestError("'filters' must be a JSON list")
        return jsonify(db.rows(
            name,
            search=args.get("search", ""),
            search_column=args.get("searchColumn") or None,
            filters=filters,
            sort=args.get("sort") or None,
            direction=args.get("dir", "asc"),
            page=page, page_size=page_size))

    @app.get("/api/tables/<path:name>/values")
    def values(name):
        try:
            filters = json.loads(request.args.get("filters") or "[]")
        except ValueError:
            raise RequestError("Invalid filters") from None
        if not isinstance(filters, list):
            raise RequestError("'filters' must be a JSON list")
        column = request.args.get("column")
        if not column:
            raise RequestError("'column' is required")
        return jsonify(db.distinct_values(name, column, search=request.args.get("search", ""),
                                          filters=filters))

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise RequestError("Expected a JSON object")
        return data

    @app.post("/api/tables/<path:name>/rows")
    def create_row(name):
        return jsonify(row=db.insert(name, body().get("values", {}))), 201

    @app.patch("/api/tables/<path:name>/rows/<key>")
    def update_row(name, key):
        return jsonify(row=db.update(name, key, body().get("values", {})))

    @app.delete("/api/tables/<path:name>/rows/<key>")
    def delete_row(name, key):
        deleted = db.delete(name, [key])
        if not deleted:
            raise NotFound("Row not found")
        return jsonify(deleted=deleted)

    @app.post("/api/tables/<path:name>/delete")
    def delete_rows(name):
        return jsonify(deleted=db.delete(name, body().get("keys")))

    return app


def main():
    settings = load_settings()
    app = create_app(settings)
    status = app.extensions["db"].status()
    print(f" * Database: {status['path'] or '(none found)'}"
          f"{'  [SQLCipher]' if status['encrypted'] else ''}"
          f"{'  [read-only]' if status['readOnly'] else ''}")
    if not status["ok"]:
        print(f" ! {status['error']}")
    app.run(host=settings.host, port=settings.port, debug=settings.debug,
            threaded=True, load_dotenv=False)  # config.py reads .env itself


if __name__ == "__main__":
    main()
