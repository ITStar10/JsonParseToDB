"""Tests for the DB Browser API. Run from Browser/: python -m unittest -v"""
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from app import create_app
from config import Settings, read_env_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Parser"))
from dbcreator import import_folder  # noqa: E402

try:
    sqlcipher = importlib.import_module("sqlcipher3.dbapi2")
except ImportError:  # pragma: no cover
    sqlcipher = None

PASSWORD = "p'ass #word"
PEOPLE = [
    {"firstName": "Alice", "lastName": "Smith", "city": "Austin", "zip": "00123", "isSold": True, "id": "001",
     "dateOfBirth": "1975-06-30"},
    {"firstName": "Bob", "lastName": "Jones", "city": "Boston", "email": "bob@example.com", "isSold": False,
     "dateOfBirth": "1980-01-01"},
    {"firstName": "Carla", "lastName": "100%_real", "city": "austin", "isSold": None,
     "dateOfBirth": "1989-12-31"},
    {"firstName": "dave", "lastName": "Allen", "city": "Chicago", "phone": None, "isSold": True,
     "dateOfBirth": "1990-01-01"},
]


def make_db(folder, name, password=""):
    src = Path(folder, "json-" + name)
    src.mkdir()
    (src / "people.json").write_text(json.dumps(PEOPLE), encoding="utf-8")
    out = Path(folder, name)
    import_folder(src, out, password=password)
    return out


def settings(path, password="", read_only=False):
    return Settings(db_path=path, db_password=password, host="127.0.0.1",
                    port=5000, read_only=read_only, debug=False)


class BrowserApiMixin:
    password = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = make_db(self.tmp.name, "test.sqlite3", self.password)
        self.app = create_app(settings(self.path, self.password))
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["db"].close()
        self.tmp.cleanup()

    def rows(self, **params):
        if "filters" in params:
            params["filters"] = json.dumps(params["filters"])
        res = self.client.get("/api/tables/records/rows", query_string=params)
        self.assertEqual(res.status_code, 200, res.json)
        return res.json

    def names(self, **params):
        return [r["values"]["firstName"] for r in self.rows(**params)["rows"]]

    def reopen(self):
        """Read the file with the same driver the Parser uses."""
        driver = sqlcipher if self.password else sqlite3
        conn = driver.connect(str(self.path))
        if self.password:
            conn.execute("PRAGMA key = '" + self.password.replace("'", "''") + "'")
        return conn

    # ------------------------------------------------------------ reads
    def test_status_and_tables(self):
        status = self.client.get("/api/status").json
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["encrypted"], bool(self.password))
        tables = self.client.get("/api/tables").json["tables"]
        self.assertEqual(tables, [{"name": "records", "kind": "table", "rows": 4, "readonly": False}])

    def test_schema(self):
        table = self.rows()["table"]
        self.assertEqual(table["key"], "_id")
        cols = {c["name"]: c for c in table["columns"]}
        self.assertTrue(cols["_id"]["readonly"])
        self.assertEqual(cols["isSold"]["kind"], "boolean")
        self.assertEqual(len(cols), 14)

    def test_values_preserved(self):
        alice = self.rows(search="Alice")["rows"][0]["values"]
        self.assertEqual(alice["zip"], "00123")
        self.assertEqual(alice["isSold"], 1)
        self.assertIsNone(alice["phone"])

    def test_search_all_columns_case_insensitive_and_multi_term(self):
        self.assertEqual(self.names(search="AUSTIN"), ["Alice", "Carla"])
        self.assertEqual(self.names(search="austin smith"), ["Alice"])
        self.assertEqual(self.names(search="example.com"), ["Bob"])

    def test_search_single_column(self):
        self.assertEqual(self.names(search="al", searchColumn="firstName"), ["Alice"])
        self.assertEqual(self.names(search="al"), ["Alice", "Carla", "dave"])  # "re*al*", "*Al*len"

    def test_like_wildcards_are_literal(self):
        self.assertEqual(self.names(search="%"), ["Carla"])
        self.assertEqual(self.names(search="_"), ["Carla"])

    def test_filters(self):
        f = lambda *fs: self.names(filters=[dict(zip(("column", "op", "value"), x)) for x in fs])  # noqa: E731
        self.assertEqual(f(("isSold", "equals", "1")), ["Alice", "dave"])
        self.assertEqual(f(("isSold", "is_null")), ["Carla"])
        self.assertEqual(f(("city", "equals", "AUSTIN")), ["Alice", "Carla"])
        self.assertEqual(f(("city", "not_equals", "austin")), ["Bob", "dave"])
        self.assertEqual(f(("lastName", "starts", "100%")), ["Carla"])
        self.assertEqual(f(("email", "is_empty")), ["Alice", "Carla", "dave"])
        self.assertEqual(f(("city", "contains", "o"), ("isSold", "equals", "0")), ["Bob"])

    def test_filter_groups(self):
        eq = lambda col, v: {"column": col, "op": "equals", "value": v}  # noqa: E731
        self.assertEqual(self.names(filters=[{"any": [eq("city", "boston"), eq("city", "chicago")]}]),
                         ["Bob", "dave"])
        self.assertEqual(self.names(filters=[{"all": [
            {"any": [eq("city", "austin"), eq("city", "boston")]},
            {"column": "firstName", "op": "not_contains", "value": "a"}]}]), ["Bob"])
        self.assertEqual(self.names(filters=[{"any": []}]), ["Alice", "Bob", "Carla", "dave"])
        too_many = {"any": [eq("city", str(i)) for i in range(500)]}
        res = self.client.get("/api/tables/records/rows", query_string={"filters": json.dumps([too_many])})
        self.assertEqual(res.status_code, 400)

    def test_date_detection_and_range(self):
        cols = {c["name"]: c["kind"] for c in self.rows()["table"]["columns"]}
        self.assertEqual(cols["dateOfBirth"], "date")
        self.assertEqual(cols["city"], "text")
        rng = {"all": [{"column": "dateOfBirth", "op": "gte", "value": "1980-01-01"},
                       {"column": "dateOfBirth", "op": "lt", "value": "1990-01-01"}]}
        self.assertEqual(self.names(filters=[rng]), ["Bob", "Carla"])

    def test_distinct_values(self):
        res = self.client.get("/api/tables/records/values", query_string={"column": "city"}).json
        self.assertEqual(res["values"], [{"value": "Austin", "count": 2}, {"value": "Boston", "count": 1},
                                         {"value": "Chicago", "count": 1}])
        res = self.client.get("/api/tables/records/values", query_string={"column": "email"}).json
        self.assertEqual(res["values"][0], {"value": None, "count": 3})  # NULL and '' merged
        res = self.client.get("/api/tables/records/values", query_string={
            "column": "city", "search": "o",
            "filters": json.dumps([{"column": "isSold", "op": "equals", "value": "1"}])}).json
        self.assertEqual(res["values"], [{"value": "Chicago", "count": 1}])
        self.assertEqual(self.client.get("/api/tables/records/values",
                                         query_string={"column": "nope"}).status_code, 400)

    def test_sort(self):
        self.assertEqual(self.names(sort="firstName"), ["Alice", "Bob", "Carla", "dave"])
        self.assertEqual(self.names(sort="firstName", dir="desc"), ["dave", "Carla", "Bob", "Alice"])
        self.assertEqual(self.names(sort="isSold"), ["Carla", "Bob", "Alice", "dave"])

    def test_pagination(self):
        data = self.rows(pageSize=3, page=2)
        self.assertEqual((data["total"], data["pages"], data["page"]), (4, 2, 2))
        self.assertEqual([r["values"]["firstName"] for r in data["rows"]], ["dave"])
        self.assertEqual(self.rows(pageSize=3, page=99)["page"], 2)

    def test_rejects_unknown_columns_and_ops(self):
        for params in ({"sort": "nope"}, {"searchColumn": 'x"; DROP TABLE records; --', "search": "a"},
                       {"filters": json.dumps([{"column": "city", "op": "regex", "value": "a"}])},
                       {"filters": "not json"}):
            res = self.client.get("/api/tables/records/rows", query_string=params)
            self.assertEqual(res.status_code, 400, params)
        self.assertEqual(self.client.get("/api/tables/nope/rows").status_code, 404)
        self.assertEqual(len(self.rows()["rows"]), 4)

    # ------------------------------------------------------------ writes
    def test_insert(self):
        res = self.client.post("/api/tables/records/rows", json={"values": {
            "firstName": "Eve", "zip": "00999", "isSold": True, "phone": None}})
        self.assertEqual(res.status_code, 201, res.json)
        row = res.json["row"]
        self.assertEqual(row["key"], 5)
        self.assertEqual(row["values"]["zip"], "00999")
        with self.reopen() as conn:
            self.assertEqual(conn.execute("SELECT firstName, zip, isSold FROM records WHERE _id = 5").fetchone(),
                             ("Eve", "00999", 1))

    def test_update_only_given_fields_and_null_vs_empty(self):
        res = self.client.patch("/api/tables/records/rows/1", json={"values": {"phone": "", "city": None}})
        self.assertEqual(res.status_code, 200, res.json)
        with self.reopen() as conn:
            self.assertEqual(conn.execute(
                "SELECT firstName, phone, city, zip FROM records WHERE _id = 1").fetchone(),
                ("Alice", "", None, "00123"))

    def test_update_validation(self):
        bad = [{"_id": 9}, {"isSold": 2}, {"isSold": "maybe"}, {"nope": 1}, {"city": [1]}]
        for values in bad:
            res = self.client.patch("/api/tables/records/rows/1", json={"values": values})
            self.assertEqual(res.status_code, 400, values)
        self.assertEqual(self.client.patch("/api/tables/records/rows/999",
                                           json={"values": {"city": "x"}}).status_code, 404)

    def test_delete(self):
        self.assertEqual(self.client.delete("/api/tables/records/rows/2").json["deleted"], 1)
        self.assertEqual(self.client.delete("/api/tables/records/rows/2").status_code, 404)
        res = self.client.post("/api/tables/records/delete", json={"keys": [1, 3, 999]})
        self.assertEqual(res.json["deleted"], 2)
        with self.reopen() as conn:
            self.assertEqual(conn.execute("SELECT _id FROM records").fetchall(), [(4,)])

    def test_json_body_required(self):
        res = self.client.post("/api/tables/records/delete", data='{"keys":[1]}',
                               content_type="text/plain")
        self.assertEqual(res.status_code, 415)


class PlainDatabaseTests(BrowserApiMixin, unittest.TestCase):
    pass


@unittest.skipIf(sqlcipher is None, "sqlcipher3 is not installed")
class EncryptedDatabaseTests(BrowserApiMixin, unittest.TestCase):
    password = PASSWORD


class ConfigurationErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, path, password=""):
        app = create_app(settings(path, password))
        try:
            return app.test_client().get("/api/status").json
        finally:
            app.extensions["db"].close()

    def test_missing_file(self):
        status = self.status(Path(self.tmp.name, "missing.sqlite3"))
        self.assertFalse(status["ok"])
        self.assertIn("not found", status["error"])

    def test_no_database_configured(self):
        self.assertIn("No database file found", self.status(None)["error"])

    def test_password_for_plain_file(self):
        path = make_db(self.tmp.name, "plain.sqlite3")
        self.assertIn("not encrypted", self.status(path, "x")["error"])

    @unittest.skipIf(sqlcipher is None, "sqlcipher3 is not installed")
    def test_encrypted_file_without_or_with_wrong_password(self):
        path = make_db(self.tmp.name, "enc.sqlite3", PASSWORD)
        self.assertIn("appears to be encrypted", self.status(path)["error"])
        self.assertIn("wrong", self.status(path, "nope")["error"])
        self.assertTrue(self.status(path, PASSWORD)["ok"])

    def test_unavailable_database_returns_503(self):
        app = create_app(settings(Path(self.tmp.name, "missing.sqlite3")))
        res = app.test_client().get("/api/tables")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json["code"], "database_unavailable")

    def test_read_only_mode(self):
        path = make_db(self.tmp.name, "ro.sqlite3")
        app = create_app(settings(path, read_only=True))
        client = app.test_client()
        self.assertEqual(client.get("/api/tables/records/rows").status_code, 200)
        self.assertEqual(client.patch("/api/tables/records/rows/1",
                                      json={"values": {"city": "x"}}).status_code, 400)
        self.assertEqual(client.post("/api/tables/records/delete", json={"keys": [1]}).status_code, 400)
        app.extensions["db"].close()

    def test_host_header_check(self):
        path = make_db(self.tmp.name, "host.sqlite3")
        app = create_app(settings(path))
        client = app.test_client()
        self.assertEqual(client.get("/api/status", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(client.get("/api/status", headers={"Host": "localhost:5000"}).status_code, 200)
        app.extensions["db"].close()

    def test_env_file_parsing(self):
        env = Path(self.tmp.name, ".env")
        env.write_text('# comment\nDB_PATH=data/x.sqlite3\nDB_PASSWORD=a#b c\n'
                       'export PORT="5050"\nQUOTED=\' spaced \'\n', encoding="utf-8")
        self.assertEqual(read_env_file(env), {"DB_PATH": "data/x.sqlite3", "DB_PASSWORD": "a#b c",
                                              "PORT": "5050", "QUOTED": " spaced "})


class GenericTableTests(unittest.TestCase):
    """Non-Parser schemas: rowid tables, WITHOUT ROWID tables and views."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "generic.db")
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE notes (title TEXT NOT NULL, body TEXT, score REAL DEFAULT 1.5);
                INSERT INTO notes (title, body) VALUES ('a', 'x'), ('b', NULL);
                CREATE TABLE tags (code TEXT PRIMARY KEY, label TEXT) WITHOUT ROWID;
                INSERT INTO tags VALUES ('t1', 'One');
                CREATE VIEW note_titles AS SELECT title FROM notes;
            """)
        conn.close()
        self.app = create_app(settings(self.path))
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["db"].close()
        self.tmp.cleanup()

    def test_rowid_table_crud(self):
        data = self.client.get("/api/tables/notes/rows").json
        self.assertEqual([r["key"] for r in data["rows"]], [1, 2])
        row = self.client.post("/api/tables/notes/rows", json={"values": {"title": "c"}}).json["row"]
        self.assertEqual(row["values"], {"title": "c", "body": None, "score": 1.5})
        self.assertEqual(self.client.post("/api/tables/notes/rows",
                                          json={"values": {"body": "no title"}}).status_code, 400)

    def test_without_rowid_table_uses_primary_key(self):
        data = self.client.get("/api/tables/tags/rows").json
        self.assertEqual(data["table"]["key"], "code")
        self.assertEqual(data["rows"][0]["key"], "t1")
        row = self.client.post("/api/tables/tags/rows", json={"values": {"code": "t2", "label": "Two"}}).json["row"]
        self.assertEqual(row["key"], "t2")
        res = self.client.patch("/api/tables/tags/rows/t1", json={"values": {"label": "Uno"}})
        self.assertEqual(res.json["row"]["values"]["label"], "Uno")

    def test_views_are_read_only(self):
        data = self.client.get("/api/tables/note_titles/rows").json
        self.assertTrue(data["table"]["readonly"])
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(self.client.post("/api/tables/note_titles/rows",
                                          json={"values": {"title": "x"}}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
