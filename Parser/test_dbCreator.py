import json
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import JsonParseToDB.Parser.dbcreator as dbcreator


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.input.mkdir()
        self.output = self.root / "out.sqlite3"

    def write(self, name, data):
        path = self.input / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")
        return path

    def connect(self):
        db = sqlite3.connect(self.output)
        self.addCleanup(db.close)
        return db

    def test_schema_order_and_types(self):
        item = {key: 123 for key in reversed(dbcreator.COLUMNS)}
        item.update(firstName="??", zip="00123", phone="+00123",
                    isSold=True, useOrders=[1, 2], id=2 ** 80)
        self.write("a.json", [item, {}, {"isSold": False}])
        result = dbcreator.import_folder(self.input, self.output, batch_size=2)
        self.assertEqual(result["rows"], 3)
        db = self.connect()
        info = db.execute("PRAGMA table_info(records)").fetchall()
        self.assertEqual([row[1] for row in info],
                         ["_id"] + list(dbcreator.COLUMNS))
        self.assertEqual([row[2] for row in info],
                         ["INTEGER"] + ["BOOLEAN" if key == "isSold" else "TEXT"
                                        for key in dbcreator.COLUMNS])
        row = db.execute("SELECT * FROM records WHERE _id=1").fetchone()
        self.assertEqual(row[0], 1)
        for key, value in zip(dbcreator.COLUMNS, row[1:]):
            if key == "isSold":
                self.assertEqual(value, 1)
            else:
                self.assertIsInstance(value, str)
        self.assertEqual(db.execute(
            "SELECT zip, phone, useOrders, id FROM records WHERE _id=1").fetchone(),
            ("00123", "+00123", "[1,2]", str(2 ** 80)))
        self.assertEqual(db.execute("SELECT isSold FROM records").fetchall(),
                         [(1,), (None,), (0,)])
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO records (isSold) VALUES (2)")

    def test_boolean_conversion(self):
        self.write("a.json", [{"isSold": v} for v in
                             [True, False, 1, 0, "true", "FALSE", "1", "0", None]])
        dbcreator.import_folder(self.input, self.output)
        self.assertEqual(self.connect().execute("SELECT isSold FROM records").fetchall(),
                         [(1,), (0,), (1,), (0,), (1,), (0,), (1,), (0,), (None,)])

    def test_invalid_boolean(self):
        for item in [{"isSold": "yes"}, {"isSold": 2}, {"isSold": []}]:
            with self.subTest(item=item):
                self.write("a.json", [item])
                with self.assertRaises(ValueError):
                    dbcreator.import_folder(self.input, self.output)
                self.assertFalse(self.output.exists())

    def test_extra_keys_do_not_change_schema(self):
        self.write("a.json", [{"firstName": "Alice", "extra": 123}])
        self.write("b.json", [{"lastName": "Smith", "another": {"x": 1}}])
        dbcreator.import_folder(self.input, self.output)
        db = self.connect()
        self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(records)")],
                         ["_id"] + list(dbcreator.COLUMNS))
        self.assertEqual(db.execute(
            "SELECT firstName, lastName FROM records ORDER BY _id").fetchall(),
            [("Alice", None), (None, "Smith")])

    def test_password_prompt(self):
        with patch("dbcreator.getpass.getpass", return_value="") as prompt:
            self.assertEqual(dbcreator.prompt_password(), "")
            prompt.assert_called_once()
        with patch("dbcreator.getpass.getpass", side_effect=["secret", "secret"]):
            self.assertEqual(dbcreator.prompt_password(), "secret")
        with patch("dbcreator.getpass.getpass", side_effect=["secret", "different"]):
            with self.assertRaisesRegex(ValueError, "do not match"):
                dbcreator.prompt_password()
        with patch("dbcreator.getpass.getpass", side_effect=EOFError):
            with self.assertRaisesRegex(ValueError, "No password input"):
                dbcreator.prompt_password()
        with patch("dbcreator.getpass.getpass", side_effect=dbcreator.getpass.GetPassWarning):
            with self.assertRaisesRegex(ValueError, "Hidden password"):
                dbcreator.prompt_password()

    def test_encrypted_roundtrip_and_wrong_password(self):
        from sqlcipher3 import dbapi2 as cipher
        password = "test'quoted-??-password"
        path = self.write("a.json", [{"firstName": "encrypted sample", "isSold": True}])
        result = dbcreator.import_folder(self.input, self.output, password=password)
        self.assertTrue(result["encrypted"])
        self.assertFalse(self.output.read_bytes().startswith(b"SQLite format 3"))
        plain = sqlite3.connect(self.output)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                plain.execute("SELECT * FROM records").fetchall()
        finally:
            plain.close()
        original = self.output.read_bytes()
        for wrong in ("wrong", ""):
            with self.assertRaisesRegex(ValueError, "Database operation failed"):
                dbcreator.import_folder(self.input, self.output, append=True, password=wrong)
            self.assertEqual(self.output.read_bytes(), original)
        dbcreator.import_folder(self.input, self.output, append=True, password=password)
        path.write_text('[{"city":"rollback"}, invalid]', encoding="utf-8")
        with self.assertRaises(ValueError):
            dbcreator.import_folder(self.input, self.output, append=True,
                                    password=password, batch_size=1)
        db = cipher.connect(str(self.output))
        try:
            dbcreator.set_password(db, password)
            self.assertEqual(db.execute(
                "SELECT _id, firstName, isSold, city FROM records").fetchall(),
                [(1, "encrypted sample", 1, None), (2, "encrypted sample", 1, None)])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            db.close()

    def test_blank_password_uses_plain_sqlite(self):
        self.write("a.json", [{"firstName": "plain"}])
        with patch("dbcreator.importlib.import_module",
                   side_effect=AssertionError("SQLCipher should not be loaded")):
            result = dbcreator.import_folder(self.input, self.output, password="")
        self.assertFalse(result["encrypted"])
        self.assertEqual(self.connect().execute("SELECT firstName FROM records").fetchone(),
                         ("plain",))

    def test_encryption_unavailable_fails_without_output(self):
        self.write("a.json", [])
        with patch("dbcreator.importlib.import_module", side_effect=ImportError):
            with self.assertRaisesRegex(ValueError, "Encryption requires"):
                dbcreator.import_folder(self.input, self.output, password="secret")
        self.assertFalse(self.output.exists())
        with patch("dbcreator.importlib.import_module", return_value=sqlite3):
            with self.assertRaisesRegex(ValueError, "does not support SQLCipher"):
                dbcreator.import_folder(self.input, self.output, password="secret")
        self.assertFalse(self.output.exists())

    def test_failed_encrypted_import_removes_output(self):
        self.write("a.json", [{"isSold": "invalid"}])
        with self.assertRaises(ValueError):
            dbcreator.import_folder(self.input, self.output, password="test password")
        self.assertFalse(self.output.exists())

    def test_password_cannot_silently_encrypt_existing_plain_file(self):
        self.write("a.json", [{"id": "original"}])
        dbcreator.import_folder(self.input, self.output)
        original = self.output.read_bytes()
        with self.assertRaises(ValueError):
            dbcreator.import_folder(self.input, self.output, append=True, password="secret")
        self.assertEqual(self.output.read_bytes(), original)

    def test_old_schema_rejected(self):
        self.write("a.json", [])
        db = sqlite3.connect(self.output)
        db.execute(f"PRAGMA application_id = {dbcreator.APP_ID}")
        db.execute("PRAGMA user_version = 1")
        db.execute("CREATE TABLE records (_id INTEGER PRIMARY KEY)")
        db.close()
        with self.assertRaisesRegex(ValueError, "create a new database"):
            dbcreator.import_folder(self.input, self.output, append=True)

    def test_append_and_rollback(self):
        path = self.write("a.json", [{"firstName": "one"}])
        dbcreator.import_folder(self.input, self.output)
        with self.assertRaises(ValueError):
            dbcreator.import_folder(self.input, self.output)
        path.write_text('[{"lastName":"two"}]', encoding="utf-8")
        dbcreator.import_folder(self.input, self.output, append=True)
        path.write_text('[{"city":"three"}, invalid]', encoding="utf-8")
        with self.assertRaises(ValueError):
            dbcreator.import_folder(self.input, self.output, append=True, batch_size=1)
        db = self.connect()
        self.assertEqual(db.execute("SELECT _id FROM records").fetchall(), [(1,), (2,)])
        self.assertEqual(db.execute("SELECT city FROM records").fetchall(), [(None,), (None,)])

    def test_invalid_json_leaves_no_database(self):
        path = self.input / "bad.json"
        for value in ['{}', '[1]', '[{},]', '[{}', '[{}]junk',
                      '[{"x":NaN}]', '[{"x":1,"x":2}]', '[{"firstName":1e999}]',
                      '[{"x":[1,]}]']:
            with self.subTest(value=value):
                path.write_text(value, encoding="utf-8")
                with self.assertRaises(ValueError):
                    dbcreator.import_folder(self.input, self.output)
                self.assertFalse(self.output.exists())

    def test_stream_boundaries_and_recursion(self):
        value = {"address": 'quote" slash\\ bracket]} 東京' * 100}
        self.write("nested/a.JSON", [value, {}, {"email": None}])
        with self.assertRaises(ValueError):
            dbcreator.import_folder(self.input, self.output)
        with patch.object(dbcreator, "CHUNK_SIZE", 7):
            result = dbcreator.import_folder(self.input, self.output, recursive=True)
        self.assertEqual(result["rows"], 3)
        self.assertEqual(self.connect().execute("SELECT address FROM records WHERE _id=1")
                         .fetchone()[0], value["address"])

    def test_cli_multiple_files(self):
        self.write("a.json", [{"firstName": 1}])
        self.write("b.json", [{"lastName": "two"}])
        result = subprocess.run(
            [sys.executable, str(Path(dbcreator.__file__)), str(self.input),
             "--output", str(self.output), "--no-password"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Imported 2 rows from 2 files", result.stdout)
        self.assertEqual(self.connect().execute(
            "SELECT _id, firstName, lastName FROM records").fetchall(),
            [(1, "1", None), (2, None, "two")])

    def test_empty_array_and_unrelated_database(self):
        self.write("a.json", [])
        self.assertEqual(dbcreator.import_folder(self.input, self.output)["rows"], 0)
        other = self.root / "other.sqlite3"
        with sqlite3.connect(other) as db:
            db.execute("CREATE TABLE unrelated (x)")
        db.close()
        with self.assertRaises(ValueError):
            dbcreator.import_folder(self.input, other, append=True)
        self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()

