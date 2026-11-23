# Versions

All notable changes to the DB Browser are listed here, newest first.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
version numbers follow [Semantic Versioning](https://semver.org/):
MAJOR.MINOR.PATCH, where before 1.0.0 new features or breaking changes raise MINOR
and fixes or changes that don't affect how the app works raise PATCH.

## [0.3.0] - 2026-11-23

### Added

- `SESSION_HOURS` setting for how long a sign-in lasts (default 12).
- README: security model section.

### Changed

- Sessions are recorded on the server (token stored as a hash). Existing sign-ins end
  when upgrading, so everyone signs in once more.

### Security

- Signing out ends the session at once, so a copied cookie stops working.
- Disabling a user, changing their role, resetting their password or deleting the account
  ends all of that user's sessions; changing your own password ends your other sessions.
- On the server, `.env`, `data/` and `instance/` are readable only by the account running the app.

## [0.2.1] - 2026-11-23

### Added

- `deploy/` folder: systemd unit and nginx site for hosting at `https://<server IP>:9443`
  (HTTPS with a self-signed certificate; `http://` redirects to `https://`).
- The service runs the app with waitress on `127.0.0.1:8765`, starts at boot, restarts on failure,
  and can only write to `data/` and `instance/`.
- nginx rate limits sign-in and sign-up.
- `waitress` in `requirements.txt`; README "Deployment on the VPS" section.

## [0.2.0] - 2026-11-23

### Added

- Sign-up with full name, e-mail and password; requests stay pending until an admin approves them.
- Sign-in and sign-out, change own password; `manage.py` to create the first admin and reset passwords.
- Users page for admins: approve, reject, disable, change role, reset password, delete accounts.
- Rows shown to users: the admin filters the Data page and clicks "Show to users…"
  (matching rows, all rows or hide the table).
- Random rows per user: each user sees N random rows (their own stable selection; the admin
  can reshuffle). Empty means all rows.
- Visible columns: the admin chooses which columns users can see. Hidden columns are never sent
  and can't be filtered, sorted or searched.
- Editable columns: users can update only the columns an admin allows (default `curUser`).
- Per-user exceptions when approving or via "Data access".
- Per-column filter boxes: multiple values, exclusions, comparisons, value checklist with counts,
  date ranges with a picker.
- Settings `USERS_DB_PATH`, `SECRET_KEY`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUST_PROXY`
  for hosting behind a proxy.
- The accounts database upgrades itself on start.

### Changed

- **Breaking:** signing in is now required, and regular users can no longer add or delete rows.
- All access rules are enforced on the server; a rule that can't be applied denies access
  instead of showing everything.

## [0.1.0] - 2026-11-18

### Added

- Show the SQLite file set in `.env`.
- Filter, add, update and delete rows.
