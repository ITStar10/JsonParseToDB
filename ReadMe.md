# DBCreator

A small JSON-to-SQLite importer. Requires Python 3.10 or newer.
Unencrypted imports use Python's built-in SQLite. Encryption requires the optional SQLCipher driver.

## Run directly with Python

```powershell
python dbcreator.py "G:\\path\\to\\json-folder"
python dbcreator.py "G:\\path\\to\\json-folder" -o "G:\\path\\to\\backend.sqlite3"
python dbcreator.py "G:\\path\\to\\new-files" -o backend.sqlite3 --append
python dbcreator.py "G:\\path\\to\\json-folder" --recursive
```

If Python is managed by uv, replace `python` with `uv run --python 3.12`.

The default output is `data.sqlite3` inside the input folder. Output parent
directories must already exist. By default only immediate files with a
case-insensitive `.json` extension are included, sorted by path.

## Optional database password

Run `python dbcreator.py` and enter your folder and output paths. The program
then prompts for a password with hidden input:

- Press Enter to create a regular, unencrypted SQLite database.
- Enter and confirm a password to create an encrypted SQLCipher database.

Install the optional encryption driver for the Python interpreter you use:

```powershell
python -m pip install -r requirements-encryption.txt
```

For unattended unencrypted imports, explicitly skip the prompt:

```powershell
python dbcreator.py "C:\path\to\json-folder" -o backend.sqlite3 --no-password
```

There is no command-line password argument and the password is not saved in
project files. EOF or unsupported hidden entry causes an error, not an automatic
unencrypted import. For `--append`, enter the existing database password, or
leave it blank for an unencrypted file. This does not change file encryption.

Encrypted files require SQLCipher and the same password in the backend or
database viewer; Python's standard `sqlite3` cannot read them. Encryption does
not alter or protect the original JSON files.

Driver: https://github.com/coleifer/sqlcipher3
SQLCipher API: https://www.zetetic.net/sqlcipher/sqlcipher-api/

## Input and database schema

Each UTF-8 JSON file must contain an array of objects. All files feed one
`records` table. Column order is fixed:

```text
_id INTEGER PRIMARY KEY AUTOINCREMENT
firstName TEXT
lastName TEXT
phone TEXT
address TEXT
city TEXT
state TEXT
zip TEXT
email TEXT
dateOfBirth TEXT
ssn TEXT
useOrders TEXT
isSold BOOLEAN
id TEXT
```

The generated `_id` is independent of the JSON `id`. All thirteen JSON fields
are created even for an empty array. Input object key order does not matter.
Missing values and JSON null become SQL NULL. Empty objects still create rows.
Extra JSON keys are ignored. No columns are added while parsing files.

Strings are preserved exactly, including leading zeros. Other JSON values
in TEXT fields are serialized as JSON text, including numbers, booleans,
objects and arrays. Supply identifiers and dates as JSON strings to preserve
their original formatting.

SQLite stores `isSold` as integer 1/0 with a CHECK constraint restricting
non-null values to those two values. Accepted input values are JSON true/false,
integer 1/0, strings "true"/"false" (case insensitive), strings "1"/"0", and null.
Other values fail the import.

Example input:

```json
[
  {"firstName": "Alice", "lastName": "Smith", "zip": "00123", "isSold": false, "id": "001"},
  {"firstName": "Bob", "email": "bob@example.com", "isSold": true}
]
```

## Performance and existing databases

Files are read incrementally and inserts are batched (default 1,000 rows;
override with `--batch-size`). The entire import uses one transaction.
Errors roll back the run. A failed new import removes its newly created output.
Memory scales with the largest object, current batch and file list, rather than
total input size. Very large individual objects may require repeated decoding.

Existing outputs require `--append`. Reimporting files adds duplicate rows.
Append accepts only databases with this fixed schema (schema version 2).
Databases created by the previous dynamic-schema version are not modified:
choose a new output filename and reimport the original JSON files.

## Tests

```powershell
python -m unittest -v
```


