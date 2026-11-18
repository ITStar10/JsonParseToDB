"""Settings loaded from Browser/.env (real environment variables win)."""
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DB_SUFFIXES = (".sqlite", ".sqlite3", ".db")


def read_env_file(path=ENV_FILE):
    """Minimal .env parser: KEY=VALUE lines, '#' comment lines, optional quotes.

    Unquoted values are taken literally up to the end of the line, so a
    password may contain '#'. Quote values that need leading/trailing spaces.
    """
    values = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = raw.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _flag(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    db_path: Path | None
    db_password: str
    host: str
    port: int
    read_only: bool
    debug: bool


def discover_database():
    """First database-looking file in Browser/ or Browser/data/, sorted by name."""
    for folder in (BASE_DIR, BASE_DIR / "data"):
        if folder.is_dir():
            found = sorted(p for p in folder.iterdir()
                           if p.is_file() and p.suffix.lower() in DB_SUFFIXES)
            if found:
                return found[0]
    return None


def load_settings():
    env = read_env_file()

    def get(key, default=""):
        return os.environ.get(key, env.get(key, default))

    db_path = get("DB_PATH").strip()
    if db_path:
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path  # relative to Browser/, not the CWD
        path = path.resolve()
    else:
        path = discover_database()
    return Settings(
        db_path=path,
        db_password=get("DB_PASSWORD"),
        host=get("HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(get("PORT", "5000").strip() or 5000),
        read_only=_flag(get("READ_ONLY", "false")),
        debug=_flag(get("FLASK_DEBUG", "false")),
    )
