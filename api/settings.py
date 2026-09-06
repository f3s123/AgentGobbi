"""Validated deployment settings; production never silently falls back to local storage."""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class Settings:
    production: bool
    database_url: str
    origin: str
    hosts: tuple[str, ...]
    agent_api_key: str = ""
    max_body: int = 65536
    result_ttl: int = 86400
    max_runs: int = 50

    @classmethod
    def from_env(cls):
        mode = os.getenv("APP_ENV", "development")
        if mode not in ("development", "test", "production"):
            raise RuntimeError("Invalid APP_ENV")
        production = mode == "production"
        origin = os.getenv("APP_ORIGIN", "http://127.0.0.1:8000").rstrip("/")
        parsed = urlparse(origin)
        if not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise RuntimeError("APP_ORIGIN must be a single origin")
        database = os.getenv("DATABASE_URL", "")
        if production:
            if parsed.scheme != "https" or not database.startswith("postgresql+psycopg://"):
                raise RuntimeError("Production requires HTTPS APP_ORIGIN and PostgreSQL DATABASE_URL")
            query = make_url(database).query
            if query.get('sslmode') != 'verify-full' or not query.get('sslrootcert'):
                raise RuntimeError("Production database requires certificate-verified TLS")
        elif not database:
            runtime = Path(__file__).resolve().parent.parent / "runtime"
            runtime.mkdir(exist_ok=True)
            database = "sqlite:///" + (runtime / "app.db").as_posix()
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError("Invalid origin scheme")
        agent_api_key = os.getenv("AGENT_API_KEY", "")
        if production and len(agent_api_key) < 32:
            raise RuntimeError("Production requires an AGENT_API_KEY of at least 32 characters")
        hosts = ((parsed.hostname,) if production
                 else tuple(dict.fromkeys((parsed.hostname, "localhost", "127.0.0.1", "::1"))))
        return cls(production, database, origin, hosts, agent_api_key)
