V0.1.0 - Initial Version (2026/11/18)

- Show sqlite file specified in the .env file
- Filter, add, update, delete implemented

V0.2.0 - Accounts, data access & filtering (2026/11/23)

- Sign up with full name, e-mail and password; requests stay pending until an admin approves them
- Sign in / sign out, change own password; manage.py to create the first admin and reset passwords
- Users page for admins: approve, reject, disable, change role, reset password, delete accounts
- Rows shown to users: admin filters the Data page and clicks "Show to users…" (matching rows, all rows or hide table)
- Random rows per user: each user sees N random rows (own stable selection, admin can reshuffle); empty = all
- Visible columns: admin chooses which columns users can see; hidden columns are never sent and can't be filtered, sorted or searched
- Editable columns: users can update only the columns an admin allows (default: curUser) and can never add or delete rows
- Per-user exceptions on approval or via "Data access"
- All access rules enforced on the server; a rule that can't be applied denies access instead of showing everything
- Per-column filter boxes: multiple values, exclusions, comparisons, value checklist with counts, date ranges with picker
- Settings: USERS_DB_PATH, SECRET_KEY, COOKIE_SECURE, ALLOWED_HOSTS, TRUST_PROXY for hosting behind a proxy
- Accounts database upgrades itself on start

V0.2.1 - Hosting on VPS (2026/11/23)

- Hosted at https://<server IP>:9443 behind nginx (HTTPS, self-signed certificate)
- App runs with waitress as a systemd service (dbbrowser) on 127.0.0.1:8765; starts at boot, restarts on failure
- Service sandboxed: only data/ and instance/ are writable
- nginx redirects http:// to https:// and rate limits sign-in / sign-up
- deploy/ folder: systemd unit and nginx site files; README "Deployment on the VPS" section
- waitress added to requirements.txt

