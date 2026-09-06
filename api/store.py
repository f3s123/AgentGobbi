"""Small transactional state store: PostgreSQL in AWS, SQLite for local tests/demo."""
from contextlib import contextmanager
import hashlib
import json
import logging
import time

from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text,
                        create_engine, delete, insert, select, update)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

metadata = MetaData()
states = Table("states", metadata, Column("id", String(128), primary_key=True),
               Column("body", Text, nullable=False))
rates = Table("rates", metadata, Column("id", String(128), primary_key=True),
              Column("count", Integer, nullable=False), Column("expires", Float, nullable=False))
audit = Table("audit", metadata, Column("id", String(64), primary_key=True),
              Column("created", Float, nullable=False), Column("owner", String(128)),
              Column("event", String(64)), Column("detail", Text))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def empty_state():
    return {"policy": None, "runs": {}, "drafts": {}, "grants": {}, "revision": 0,
            "evaluation": {"permission": "AUTO", "history": [], "requests": {}}}


class Store:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True,
                                   connect_args={"check_same_thread": False, "timeout": 10}
                                   if url.startswith("sqlite:") else {"connect_timeout": 5})

    def initialize(self):
        # Run as a one-off migration command in AWS, not with runtime DDL privileges.
        metadata.create_all(self.engine)
        with self.transaction() as conn:
            make = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
            conn.execute(make(states).values(id="service", body=canonical(empty_state()))
                         .on_conflict_do_nothing(index_elements=[states.c.id]))

    @contextmanager
    def transaction(self):
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.begin()
                conn.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                conn.exec_driver_sql("SET LOCAL statement_timeout = '60s'")
            try:
                conn.info['audit_records'] = []
                yield conn
                conn.commit()
                for record in conn.info.pop('audit_records', []):
                    logging.getLogger('security.audit').info(canonical(record))
            except BaseException:
                conn.rollback()
                conn.info.pop('audit_records', None)
                raise

    def rate(self, key, limit, seconds=60):
        now = time.time()
        bucket = digest(key) + ":" + str(int(now // seconds))
        with self.transaction() as conn:
            conn.execute(delete(rates).where(rates.c.expires < now))
            make = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
            stmt = make(rates).values(id=bucket, count=1, expires=now + seconds * 2)
            count = conn.execute(stmt.on_conflict_do_update(
                index_elements=[rates.c.id], set_={"count": rates.c.count + 1}
            ).returning(rates.c.count)).scalar_one()
        return count <= limit

    def load(self, conn, owner):
        row = conn.execute(select(states.c.body).where(states.c.id == owner).with_for_update()).scalar_one()
        return json.loads(row)

    def save(self, conn, owner, state):
        conn.execute(update(states).where(states.c.id == owner).values(body=canonical(state)))

    def event(self, conn, owner, event, **detail):
        import secrets
        # Callers pass only allowlisted metadata, never bodies, credentials, or exception strings.
        record = dict(id=secrets.token_hex(16), created=time.time(), owner=owner,
                      event=event, detail=canonical(detail))
        conn.execute(insert(audit).values(**record))
        conn.info.setdefault('audit_records', []).append(record)

    def cleanup(self):
        with self.transaction() as conn:
            conn.execute(delete(audit).where(audit.c.created < time.time() - 90 * 86400))
            owners = conn.execute(select(states.c.id)).scalars().all()
        for owner in owners:
            with self.transaction() as conn:
                state = self.load(conn, owner)
                now = time.time()
                state['runs'] = {k: v for k, v in state['runs'].items() if v.get('expires_at', 0) > now}
                for key in ('drafts', 'grants'):
                    state[key] = {k: v for k, v in state[key].items() if v['expires'] > now}
                ev = state['evaluation']
                ev['history'] = [h for h in ev['history'] if h['ts'] > now - 86400]
                ev['requests'] = {k: v for k, v in ev['requests'].items() if v.get('expires', 0) > now}
                self.save(conn, owner, state)
