# DB Browser

A web app for browsing and editing the SQLite databases produced by
`Parser/dbcreator.py`, including SQLCipher password-protected ones. It also
works with any other SQLite file.

- **Admins** approve sign-ups, decide which rows and columns regular users can
  see, and which columns they may edit.
- **Users** request access, then browse, filter and (where allowed) update the
  data an admin shares with them.

Everything runs in Python (Flask). The pages are plain HTML/JS with no build
step. The app edits the configured database file **in place**; nothing is
copied. Back the file up regularly.

## Quick start

Requires Python 3.10+. From this `Browser` folder:

```powershell
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env
# edit .env: DB_PATH (and DB_PASSWORD for an encrypted file)
python manage.py create-admin     # prompts for name, e-mail and password
python app.py
```

Open <http://127.0.0.1:5000> and sign in as the admin. Other people use
**Request access** on the same page.

`sqlcipher3` is only needed for encrypted databases. If it fails to install on
your platform and you only use plain SQLite files, remove it from
`requirements.txt`.

## Configuration (`.env`)

Real environment variables override `.env`. Comments must be on their own line;
a `#` after a value is part of the value.

| Setting | Default | Meaning |
|---|---|---|
| `DB_PATH` | first `*.sqlite`, `*.sqlite3` or `*.db` in `Browser/` or `Browser/data/` | Data file. Relative paths start at `Browser/`. |
| `DB_PASSWORD` | empty | Password for SQLCipher-encrypted files (the one entered in `dbcreator.py`). Leave empty for plain files. Used literally, including `#`; quote it only for leading/trailing spaces. |
| `HOST` | `127.0.0.1` | Address to listen on. See [Hosting](#hosting) before changing it. |
| `PORT` | `5000` | Port to listen on. |
| `READ_ONLY` | `false` | `true` disables every change, for admins too. |
| `USERS_DB_PATH` | `instance/users.sqlite3` | Accounts and admin settings, kept separate from the data file. |
| `SECRET_KEY` | generated once into `instance/secret_key` | Signs session cookies. Changing it signs everyone out. |
| `COOKIE_SECURE` | `false` | Set `true` when the site is served over HTTPS. |
| `ALLOWED_HOSTS` | empty | Extra host names or IPs users type in the address bar (comma separated). Needed when serving to other machines. |
| `SESSION_HOURS` | `12` | How long a sign-in lasts (1–720 hours). |
| `TRUST_PROXY` | `false` | `true` only when exactly one reverse proxy is in front; takes the client IP and scheme from its `X-Forwarded-*` headers. |

- Admins can edit `.env` while the app runs and click ↻ in the top bar (or
  **Reload .env & retry** on the error screen) to reconnect to the data file.
- A wrong or missing `DB_PASSWORD`, or one set for an unencrypted file, is
  explained on the page.
- `.env`, database files and `instance/` are git-ignored because they hold
  personal data and secrets. Keep it that way.

## Accounts

- **Sign up:** anyone can request access with their full name, e-mail and a
  password (at least 8 characters). The request stays **pending** until an
  admin approves it. Pending, rejected and disabled accounts can't sign in.
- **Admins** see and edit everything, and manage users on the **Users** page
  (`/admin`): approve or reject requests, disable or enable accounts, make
  someone an admin or a regular user, reset passwords, delete accounts.
  The last admin can't be disabled, demoted or deleted.
- **Users** see only what admins share, can update only the columns admins
  allow, and can never add or delete rows.
- Disabling a user, changing their role or resetting their password signs out
  their open sessions straight away. Changing your own password keeps your
  current session and ends the others.
- Passwords are stored as salted hashes (Werkzeug scrypt). Sign-in and sign-up
  are rate limited per client IP.

### Security model

- **Every API call needs a live session** except sign-up, sign-in, sign-out and
  "who am I". The session cookie is signed with `SECRET_KEY`, `HttpOnly`,
  `SameSite=Lax`, `Secure` with `COOKIE_SECURE=true`, and only carries a random
  token.
- **Sessions are recorded on the server** (`sessions` table, token stored as a
  SHA-256 hash). Each request checks the token is still on record and not
  expired (`SESSION_HOURS`).
  - Signing out deletes the record, so a copied cookie stops working at once.
  - Disabling a user, changing their role, resetting their password or deleting
    the account ends all their sessions.
  - Changing your own password ends your other sessions.
- **Rules are checked on the server on every request**: rows, columns, edits and
  admin-only actions. Requests from other sites are refused (Origin check, JSON
  bodies).
- **Approved users can copy what they are allowed to see**, for example by
  paging through the API. Limit that with the rows, random rows and visible
  columns you share.
- **Keep secrets private on the server:** `.env`, `data/` and `instance/` should
  be readable only by the account that runs the app (`chmod 600 .env`,
  `chmod 700 data instance`).

From the command line (in `Browser/`):

```powershell
python manage.py create-admin                        # first admin, or another one
python manage.py list-users
python manage.py reset-password someone@example.com
```

## Admin guide: what users see and edit

All settings below apply to every regular user immediately, including people
already signed in. Admins always see and edit everything.

### Rows shown to users

On the **Data** page:

1. Filter the table with the column filter boxes (see [Filtering](#filtering)).
2. Click **Show to users…** and choose **Show these N rows**, **Show all
   rows** or **Hide table**.

Users then see only those rows and can filter, sort and search within them.
The saved rule is the filter text, not a list of rows, so rows added or changed
later that match the filters are shown automatically. The page subtitle and the
**Data shown to users** card on the Users page show the current setting.

### Random rows per user

Set **Random rows per user** on the Users page (or **Random selection** in the
**Show to users…** dialog). Each user then sees only that many random rows
among the rows shown to users. Leave it empty to show all.

- Each user gets their own selection, which stays the same between visits and
  across pages.
- **Pick new random rows** gives everyone a fresh selection. Changing only the
  number keeps the existing picks (raising it adds rows).
- Sampling needs a table with a row key. Its cost grows with the number of
  matching rows: roughly 0.05 s per query for 30,000 rows, 0.3 s for 300,000.

### Visible columns

**Users → Visible columns** chooses which columns users can see (default: all).

- Hidden columns are never sent to users, and users can't filter, sort, search
  or list values on them; the server answers "Unknown column", the same as for
  a column that doesn't exist.
- Your row filters may still use hidden columns, e.g. show only TX rows without
  showing `state`.
- Once a table is restricted, columns added to the data later stay hidden until
  you tick them. **Show all columns** returns to the default.
- On the admin Data page, hidden columns are marked with a crossed-out eye.

### Editable columns

**Users → Editable columns** chooses which columns users may update.

- Default: `curUser`, in any table that has a column with that name. (The
  Parser's current fixed schema doesn't create one; add it to your data or pick
  other columns.)
- A column must be visible to be editable; hidden columns are greyed out with a
  link to **Visible columns**.
- Users can only edit rows they can currently see. Row keys, generated and
  binary columns can't be made editable.
- If an editable column is also used in the row filter, a user can edit a row so
  it no longer matches, and it leaves their view.

### Per-user exceptions

**Approve…** (for a new request) or **Data access** (for an existing user) gives
one person different rows for a table: **Same as all users** (the default),
**All rows**, **Only matching rows** (column filters with a live row count) or
**No access**, each optionally with its own random number of rows. Per table,
the user's own rule wins over the rows shown to all users.

### How it is enforced

Everything is checked on the server for every request: rows, row counts, value
lists and edits. A user's own filters are combined with the admin's rules, so
they can only narrow their view. If a rule can't be applied (for example a
column was renamed), users are denied that table rather than shown all of it.

## Filtering

Each column has a filter box under its header. Filters on several columns
combine (AND). Plain text means "contains" and ignores case. You can also type:

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

- The ▾ button opens a checklist of the column's values with row counts, like a
  spreadsheet filter; it respects the other columns' filters (`Alt+↓`).
- Date columns get a 📅 button with From / To pickers and decade presets. A
  column is a date column when its declared type is DATE or TIME, or its values
  look like `YYYY-MM-DD` (e.g. `dateOfBirth`). A partial end date such as
  `..1975` or `..1975-06` includes that whole year or month.
- Boolean columns such as `isSold` get an All / Yes / No / NULL dropdown.
- Click a column header to sort: ascending, descending, off. **Columns** shows
  or hides columns in your own view.
- Shortcuts: `/` jumps to the first filter box, `Esc` clears a box or closes a
  panel, `Ctrl+Enter` saves the open row.

Admins can also add rows, edit any column, and delete rows (one at a time or
with the checkboxes). NULL and empty text are kept distinct, and only changed
fields are saved.

## Hosting

By default the app listens on `127.0.0.1` only, so only this machine can reach
it. To let others connect:

1. Run a production server instead of `python app.py` (Flask's development
   server). From this folder:

   ```powershell
   pip install waitress
   waitress-serve --host 127.0.0.1 --port 5000 --call app:create_app
   ```

   Run a single waitress process; the rate limits are kept in memory. On Linux,
   wrap it in a systemd service so it restarts after reboots.

2. **Recommended: HTTPS reverse proxy.** Point a domain at the server and put
   Caddy, nginx or IIS in front of `127.0.0.1:5000`. With Caddy the whole
   config can be `data.example.com { reverse_proxy 127.0.0.1:5000 }`. Then set:

   ```ini
   ALLOWED_HOSTS=data.example.com
   TRUST_PROXY=true
   COOKIE_SECURE=true
   ```

   Without `ALLOWED_HOSTS` every request through the proxy gets 403. Without
   `TRUST_PROXY` everyone appears to come from the proxy's IP and shares one
   rate limit.

3. **Server IP and port only.** Possible, but plain `http://<ip>:<port>` sends
   passwords and data unencrypted. Prefer a proxy with HTTPS on the IP (e.g.
   Caddy with a self-signed certificate, which browsers warn about once), and
   open the port in the firewall only to your users' IP addresses. For a short
   test with fake data you can set `HOST=0.0.0.0` and
   `ALLOWED_HOSTS=<server IP>`.

Alternatives for a small known group: a VPN such as Tailscale, or VS Code's
port forwarding for a temporary demo.

### Deployment on the VPS (`deploy/`)

The app is hosted at `https://<server IP>:9443` with the files in `deploy/`:

| File | Installed as | Purpose |
|---|---|---|
| `dbbrowser.service` | `/etc/systemd/system/dbbrowser.service` | Runs waitress on `127.0.0.1:8765`, starts at boot, restarts on failure. Sandboxed: only `data/` and `instance/` are writable. |
| `nginx-dbbrowser.conf` | `/etc/nginx/sites-available/dbbrowser` (+ link in `sites-enabled`) | HTTPS on port 9443, redirects `http://` to `https://`, rate limits sign-in/sign-up. |
| `nginx-dbbrowser-proxy.conf` | `/etc/nginx/dbbrowser-proxy.conf` | Proxy headers the app trusts with `TRUST_PROXY=true`. |

The certificate is self-signed (`/etc/nginx/dbbrowser-tls/`), so each browser
shows a one-time warning that users accept. `.env` contains
`HOST=127.0.0.1`, `PORT=8765`, `ALLOWED_HOSTS=<server IP>`, `TRUST_PROXY=true`
and `COOKIE_SECURE=true`.

Day-to-day commands (as root):

```bash
systemctl restart dbbrowser            # after changing code or .env
systemctl status dbbrowser
journalctl -u dbbrowser -f             # live log
nginx -t && systemctl reload nginx     # after changing the nginx files
```

To set it up on another server: install the three files (adjust paths and the
IP), create the certificate with
`openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout server.key -out server.crt -subj "/CN=DB Browser" -addext "subjectAltName=IP:<server IP>"`,
then `systemctl daemon-reload && systemctl enable --now dbbrowser` and reload
nginx. Back up `data/` and `instance/` regularly; edits go straight into the
data file.

## Demo data

```powershell
python tools/make_sample_db.py                       # data/sample.sqlite3
python tools/make_sample_db.py --password secret     # data/sample-encrypted.sqlite3
```

This generates fake records through the real Parser, so the schema matches the
Parser's output.

## Tests

```powershell
python -m unittest -v
```

116 tests cover plain and encrypted databases (encrypted tests are skipped
without `sqlcipher3`), filtering, sorting, pagination, CRUD, error states,
non-Parser schemas, sign-up and approval, protection of every API route, row
and column access rules, random selection, editable columns, the filter syntax
parser and upgrading an older accounts database.

## Project layout

```text
app.py          Flask app, access checks and JSON API
auth.py         Accounts, approval status, access rules, column settings (users DB)
filters.py      Filter-box syntax parser (single source of truth)
database.py     Plain/SQLCipher connection, schema, safe queries, random selection
config.py       .env loading
manage.py       create-admin / reset-password / list-users
static/         index.html + app.js (data page), login.html + login.js,
                admin.html + admin.js (Users page), common.js, app.css, icons.svg
tools/          make_sample_db.py
deploy/         systemd service and nginx site used for hosting
instance/       users.sqlite3 and secret_key (created on first run, git-ignored)
test_browser.py, test_auth.py
```

Existing accounts databases are upgraded automatically on start (new tables and
columns are added; nothing is removed).

## API

All routes except `/api/auth/signup`, `/login`, `/logout` and `/me` need a
signed-in user. Rows marked **admin** also need the admin role.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/signup` | Request an account `{fullName, email, password}` |
| POST | `/api/auth/login` / `logout` | Sign in or out (session cookie) |
| GET | `/api/auth/me` | Current user |
| POST | `/api/auth/password` | Change own password `{currentPassword, newPassword}` |
| GET | `/api/status` | Connection status |
| POST | `/api/reload` | **admin**: re-read `.env` and reconnect |
| GET | `/api/tables` | Tables with row counts (within the user's access) |
| GET | `/api/tables/<t>/rows?columnFilters=&sort=&dir=&page=&pageSize=` | Query rows. `columnFilters` is a JSON object `{column: "filter-box text"}` |
| GET | `/api/tables/<t>/values?column=&search=&columnFilters=` | Distinct values of a column with counts (up to 300) |
| PATCH | `/api/tables/<t>/rows/<key>` | Update a row `{values}`. Admins: any column. Users: allowed columns in rows they can see |
| POST / DELETE | `/api/tables/<t>/rows`, `/api/tables/<t>/rows/<key>`, `/api/tables/<t>/delete` | **admin**: insert, delete one, delete many `{keys}` |
| GET | `/api/admin/users?status=` | **admin**: accounts and counts per status |
| POST | `/api/admin/users/<id>/status` · `/role` · `/password` | **admin**: approve, reject, disable; change role; reset password |
| DELETE | `/api/admin/users/<id>` | **admin**: delete an account |
| GET / PUT | `/api/admin/access-defaults` | **admin**: rows shown to all users `{rules: {table: {mode, filters, sample, reshuffle}}}` |
| GET / PUT | `/api/admin/users/<id>/access` | **admin**: one user's rules `{rules, approve}`; mode `inherit` = same as all users |
| POST | `/api/admin/access-preview` | **admin**: row count a filter would allow |
| GET / PUT | `/api/admin/visible-columns` | **admin**: columns users can see `{rules: {table: [columns] \| null}}`; `null` = all |
| GET / PUT | `/api/admin/editable-columns` | **admin**: columns users may edit `{rules: {table: [columns] \| null}}`; `null` = default |

Rows are identified by the table's `INTEGER PRIMARY KEY` (`_id` for Parser
databases), by `rowid`, or by a single-column primary key for `WITHOUT ROWID`
tables. Views and tables without a usable key are read-only.
