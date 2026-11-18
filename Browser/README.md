# DB Browser

A local web app for browsing and editing the SQLite databases produced by
`Parser/dbcreator.py`, including SQLCipher password-protected ones.
It also works with any other SQLite file.

- Each column has a filter box under its header. Filters on several columns
  combine. Plain text means "contains" and ignores case. You can also type:

  | Type | Meaning |
  |---|---|
  | `austin, boston` | any of several values (OR) |
  | `=Austin` | exactly equals (case-insensitive) |
  | `!Austin` | does not contain; several exclusions all apply (`!a, !b`) |
  | `!=Austin` | does not equal |
  | `>1980`, `>=`, `<`, `<=` | compare |
  | `1980-01-01..1989-12-31`, `1990..`, `..1975` | range, both ends inclusive |
  | `=` / `!=` | is empty / is not empty |
  | `="Main St, 2"` | quote values that contain commas |

- The ▾ button in a filter box opens a checklist of that column's distinct
  values with row counts, like a spreadsheet filter. The list respects the other
  columns' filters. `Alt+↓` opens it from the keyboard.
- Date columns get a 📅 button with From / To pickers and decade presets. A
  column counts as a date column when its declared type is DATE or TIME, or when
  its values look like ISO dates (`YYYY-MM-DD`), such as `dateOfBirth`. In date
  ranges, a partial end such as `..1975` or `..1975-06` includes that whole year
  or month.
- Boolean columns such as `isSold` get an All / Yes / No / NULL dropdown.
  `Esc` clears a box, `/` jumps to the first box, and **Clear filters** resets all of them.
- Sort by clicking a column header: ascending, then descending, then off.
- Add, edit and delete rows. Bulk delete is available with checkboxes.
  NULL and empty string are kept distinct, and only changed fields are saved.
- Show or hide columns, paginate, and switch between light and dark themes.

The browser edits the configured database file **in place**; nothing is copied.
Make a backup first if you want to be able to undo changes.

## Setup

Requires Python 3.10+. From this `Browser` folder:

```powershell
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env
```

`sqlcipher3` is only needed for encrypted databases. If it fails to install on
your platform and you only use plain SQLite files, remove it from
`requirements.txt`.

## Configure `.env`

```ini
# Relative to Browser/, or an absolute path
DB_PATH=data/backend.sqlite3
# Set only for encrypted (SQLCipher) files
DB_PASSWORD=
HOST=127.0.0.1
PORT=5000
# true disables add/edit/delete
READ_ONLY=false
```

Comments must be on their own line. A `#` after a value is treated as part of
the value.

- If `DB_PATH` is omitted, the first `*.sqlite`, `*.sqlite3` or `*.db` file in
  `Browser/` or `Browser/data/` is used.
- `DB_PASSWORD` is the password you entered in `dbcreator.py`. The whole value
  after `=` is used literally, including `#`. Quote it only if it has
  leading or trailing spaces.
- You can edit `.env` while the app is running. Click the ↻ button in the top
  bar, or **Reload .env & retry** on the error screen, to reconnect.
- If the password is wrong, missing, or set for an unencrypted file, the page
  explains what is wrong.

`.env` and database files are listed in the repository `.gitignore`. Keep it
that way, because the data can contain personal information.

## Run

```powershell
python app.py
```

Open <http://127.0.0.1:5000>. Keyboard shortcuts: `/` jumps to the first filter box, `Esc`
closes panels, and `Ctrl+Enter` saves the open row.

The server listens on localhost only and rejects requests with non-local
`Host` headers. It is a development server meant for single-user local use;
don't expose it to a network.

## Demo data

```powershell
python tools/make_sample_db.py                       # data/sample.sqlite3
python tools/make_sample_db.py --password secret     # data/sample-encrypted.sqlite3
```

This generates fake records through the real Parser, so the schema matches
your own databases.

## Tests

```powershell
python -m unittest -v
```

The tests cover plain and encrypted databases (encrypted tests are skipped if
`sqlcipher3` is missing), search/filter/sort/pagination, CRUD, error states,
and non-Parser schemas.

## Project layout

```text
app.py          Flask app and JSON API
database.py     Plain/SQLCipher connection, schema introspection, safe queries
config.py       .env loading
static/         index.html, app.css, app.js (no build step)
tools/          make_sample_db.py
test_browser.py
```

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Connection status |
| POST | `/api/reload` | Re-read `.env` and reconnect |
| GET | `/api/tables` | Tables and views with row counts |
| GET | `/api/tables/<t>/rows?filters=&sort=&dir=&page=&pageSize=` | Query rows. `filters` is a JSON list, combined with AND, of `{column, op, value}` or groups `{"any": [...]}` / `{"all": [...]}` |
| GET | `/api/tables/<t>/values?column=&search=&filters=` | Distinct values of a column with counts (up to 300) |
| POST | `/api/tables/<t>/rows` | Insert `{values: {...}}` |
| PATCH | `/api/tables/<t>/rows/<key>` | Update changed fields `{values: {...}}` |
| DELETE | `/api/tables/<t>/rows/<key>` | Delete one row |
| POST | `/api/tables/<t>/delete` | Delete many `{keys: [...]}` |

Rows are identified by the table's `INTEGER PRIMARY KEY` (`_id` for Parser
databases), by `rowid`, or by a single-column primary key for `WITHOUT ROWID`
tables. Views and tables without a usable key are shown read-only.
