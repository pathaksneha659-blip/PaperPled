import hashlib
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


TRACKING_PARAMS = {"utm_", "ref", "fbclid", "gclid"}


def canonical_url(url: str, rel_canonical: str | None = None) -> str:
    chosen = (rel_canonical or url or "").strip()
    if not chosen:
        return ""
    if rel_canonical:
        chosen = urljoin(url, chosen)
    if chosen.startswith("//"):
        chosen = "https:" + chosen
    elif "://" not in chosen:
        chosen = "https://" + chosen
    parts = urlsplit(chosen)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
        and not key.lower().startswith("utm_")
    ]
    path = parts.path or ""
    if path != "/":
        path = path.rstrip("/")
    elif path == "/":
        path = ""
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return hashlib.sha256(normalized[:5000].encode("utf-8")).hexdigest()


class SeenStore:
    """Local SQLite implementation of Redis SET NX EX + Postgres UNIQUE semantics."""

    def __init__(self, path: str | Path = "data/seen.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS seen("
            "key TEXT PRIMARY KEY, source TEXT, first_seen_at TEXT, run_id TEXT)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS runs("
            "source TEXT PRIMARY KEY, last_run_at TEXT, top_key TEXT)"
        )
        self.connection.commit()

    def claim(self, key: str, source: str, run_id: str) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO seen(key, source, first_seen_at, run_id) "
            "VALUES (?, ?, datetime('now'), ?)",
            (key, source, run_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def last_run(self, source: str):
        row = self.connection.execute(
            "SELECT last_run_at FROM runs WHERE source = ?", (source,)
        ).fetchone()
        return row[0] if row else None

    def set_run(self, source: str, top_key: str):
        self.connection.execute(
            "INSERT INTO runs(source, last_run_at, top_key) VALUES (?, datetime('now'), ?) "
            "ON CONFLICT(source) DO UPDATE SET last_run_at=excluded.last_run_at, "
            "top_key=excluded.top_key",
            (source, top_key),
        )
        self.connection.commit()