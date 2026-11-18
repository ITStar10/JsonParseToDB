#!/usr/bin/env python3
"""Stream JSON arrays into a fixed-schema SQLite database."""
import argparse
import getpass
import importlib
import warnings
import json
import sqlite3
from pathlib import Path

APP_ID = 0x44424352
CHUNK_SIZE = 65536
SCHEMA_VERSION = 2
COLUMNS = (
    "firstName", "lastName", "phone", "address", "city", "state", "zip",
    "email", "dateOfBirth", "ssn", "useOrders", "isSold", "id",
)


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def iter_items(path):
    """Read one top-level object at a time, without loading the whole array."""
    decoder = json.JSONDecoder(parse_constant=reject_constant,
                               object_pairs_hook=unique_object)
    with path.open("r", encoding="utf-8-sig") as source:
        buffer = ""
        eof = False

        def fill():
            nonlocal buffer, eof
            chunk = source.read(CHUNK_SIZE)
            buffer += chunk
            eof = not chunk

        def skip_space():
            nonlocal buffer
            while True:
                buffer = buffer.lstrip(" \t\r\n")
                if buffer or eof:
                    return
                fill()

        skip_space()
        if not buffer.startswith("["):
            raise ValueError("Expected a top-level JSON array")
        buffer = buffer[1:]
        skip_space()
        if buffer.startswith("]"):
            buffer = buffer[1:]
        else:
            while True:
                skip_space()
                if not buffer.startswith("{"):
                    raise ValueError("Each array item must be a JSON object")
                while True:
                    try:
                        item, end = decoder.raw_decode(buffer)
                        break
                    except json.JSONDecodeError:
                        if eof:
                            raise
                        fill()
                buffer = buffer[end:]
                yield item
                skip_space()
                if buffer.startswith("]"):
                    buffer = buffer[1:]
                    break
                if not buffer.startswith(","):
                    raise ValueError("Expected ',' or ']' after array item")
                buffer = buffer[1:]
        skip_space()
        if buffer:
            raise ValueError("Unexpected content after JSON array")


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def sql_value(key, value):
    if value is None:
        return None
    if key == "isSold":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int) and value in (0, 1):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1"):
                return 1
            if normalized in ("false", "0"):
                return 0
        raise ValueError("isSold must be true, false, 0, 1, or null")
    if isinstance(value, str):
        return value
    # Preserve JSON spelling for booleans and serialize structured values as text.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)



def database_driver(password):
    if not password:
        return sqlite3
    if "\x00" in password:
        raise ValueError("Passwords cannot contain NUL characters")
    try:
        return importlib.import_module("sqlcipher3.dbapi2")
    except (ImportError, OSError) as exc:
        raise ValueError(
            "Encryption requires SQLCipher. Install it with: "
            "python -m pip install -r requirements-encryption.txt"
        ) from exc


def set_password(connection, password):
    if not connection.execute("PRAGMA cipher_version").fetchone():
        raise ValueError("The installed driver does not support SQLCipher encryption")
    # PRAGMA does not accept bound parameters; escape the SQL string literal.
    connection.execute("PRAGMA key = '" + password.replace("'", "''") + "'")


def prompt_password(confirm=True):
    # Never fall back to visible password input in an unsupported terminal.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass(
                "Database password (Enter for no encryption): ")
            if password and confirm:
                if password != getpass.getpass("Confirm database password: "):
                    raise ValueError("Passwords do not match")
            return password
        except getpass.GetPassWarning:
            raise ValueError(
                "Hidden password entry is unavailable. Use an interactive terminal, "
                "or --no-password for an unencrypted database"
            ) from None
        except EOFError:
            raise ValueError(
                "No password input received; use --no-password for unattended "
                "unencrypted imports"
            ) from None


def import_folder(folder, output=None, *, recursive=False, append=False,
                  batch_size=1000, password=None):
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError(f"Input folder does not exist: {folder}")
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    paths = folder.rglob("*") if recursive else folder.iterdir()
    paths = sorted((p for p in paths if p.is_file() and p.suffix.lower() == ".json"),
                   key=lambda p: str(p))
    if not paths:
        raise ValueError(f"No JSON files found in {folder}")
    output = Path(output).resolve() if output else folder / "data.sqlite3"
    if output in paths:
        raise ValueError("Output cannot be an input JSON file")
    driver = database_driver(password)
    created = False
    connection = None
    try:
        # Exclusive creation prevents overwriting an existing output.
        try:
            with output.open("xb"):
                pass
            created = True
        except FileExistsError:
            if not append:
                raise ValueError(f"Output exists: {output}; use --append to add rows")
        connection = driver.connect(str(output), isolation_level=None)
        if password:
            set_password(connection, password)
        connection.execute("PRAGMA busy_timeout = 30000")
        if not created:
            if (connection.execute("PRAGMA application_id").fetchone()[0] != APP_ID
                    or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
                raise ValueError("Append requires the current fixed schema; create a new database from your JSON files")
        connection.execute("BEGIN IMMEDIATE")
        if created:
            connection.execute(f"PRAGMA application_id = {APP_ID}")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            definitions = [
                '"_id" INTEGER PRIMARY KEY AUTOINCREMENT',
                *(f'{quote(key)} BOOLEAN CHECK ({quote(key)} IN (0, 1))'
                  if key == "isSold" else f'{quote(key)} TEXT'
                  for key in COLUMNS),
            ]
            connection.execute("CREATE TABLE records (" + ", ".join(definitions) + ")")
        actual = [(row[1], row[2]) for row in
                  connection.execute("PRAGMA table_info(records)")]
        expected = [("_id", "INTEGER")] + [
            (key, "BOOLEAN" if key == "isSold" else "TEXT") for key in COLUMNS]
        if actual != expected:
            raise ValueError("Database columns do not match the required schema")
        count = 0
        pending = []
        columns = ", ".join(quote(key) for key in COLUMNS)
        placeholders = ", ".join("?" for _ in COLUMNS)
        insert_sql = f"INSERT INTO records ({columns}) VALUES ({placeholders})"

        def flush():
            if pending:
                connection.executemany(insert_sql, pending)
                pending.clear()

        for path in paths:
            index = 0
            try:
                for index, item in enumerate(iter_items(path), 1):
                    pending.append(tuple(sql_value(key, item.get(key)) for key in COLUMNS))
                    count += 1
                    if len(pending) >= batch_size:
                        flush()
                flush()
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"{path} (at or after item {index}): {exc}") from exc
        connection.execute("COMMIT")
        return {"files": len(paths), "rows": count, "columns": len(COLUMNS),
                "output": str(output), "encrypted": bool(password)}
    except BaseException as exc:
        if connection is not None:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
            connection = None
        if created:
            output.unlink(missing_ok=True)
        if isinstance(exc, driver.Error):
            raise ValueError(
                "Database operation failed. For existing files, check the password "
                "and encryption mode; also check file integrity and available disk space."
            ) from None
        raise
    finally:
        if connection is not None:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", type=Path, help="Folder containing JSON arrays (prompts when omitted)")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output database (default: FOLDER/data.sqlite3)")
    parser.add_argument("--recursive", action="store_true", help="Include subfolders")
    parser.add_argument("--append", action="store_true",
                        help="Append to an existing DBCreator database; no deduplication")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--no-password", action="store_true",
                        help="Skip the password prompt and use unencrypted SQLite")
    args = parser.parse_args()
    if args.folder is None:
        try:
            folder_text = input("JSON folder path: ").strip().strip('"')
            if not folder_text:
                parser.error("A JSON folder path is required")
            args.folder = Path(folder_text)
            if args.output is None:
                output_text = input(
                    "Output database path (Enter for data.sqlite3 in input folder): "
                ).strip().strip('"')
                if output_text:
                    args.output = Path(output_text)
        except EOFError:
            parser.error("No input received; provide the folder as a command-line argument")
        except KeyboardInterrupt:
            parser.exit(130, "\nCancelled.\n")
    try:
        target = args.output if args.output else args.folder / "data.sqlite3"
        password = "" if args.no_password else prompt_password(
            confirm=not (args.append and target.exists()))
        result = import_folder(args.folder, args.output, recursive=args.recursive,
                               append=args.append, batch_size=args.batch_size, password=password)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Import cancelled; changes rolled back.\n")
    print(f"Imported {result['rows']} rows from {result['files']} files "
          f"({result['columns']} JSON fields) into {result['output']}")
    print("Encryption: SQLCipher" if result["encrypted"] else "Encryption: none")


if __name__ == "__main__":
    main()


