"""Provision fresh local databases for P0-B acceptance; never fall back as a test target."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

ROOT = Path(__file__).resolve().parents[1]


class IsolatedP0BDatabase:
    """Credentials stay in memory/subprocess env. Cleanup requires the creation marker."""
    def __init__(self):
        if os.getenv("P0B_DB_TESTS") != "1":
            raise RuntimeError("P0B_DB_TESTS=1 is required")
        self.admin_url = make_url(settings.database_url)
        if self.admin_url.host not in ("localhost", "127.0.0.1", "::1"):
            raise RuntimeError("Only a local PostgreSQL server is allowed")
        self.run_id = uuid4().hex[:16]
        self.name = "grouphang_p0b_test_" + self.run_id
        self.restore_name = "grouphang_p0b_restore_" + self.run_id
        self.role = "p0b_test_" + self.run_id
        self.marker = "p0b-isolated-acceptance:" + uuid4().hex
        self.password = secrets.token_hex(32)
        self.url = self.admin_url.set(database=self.name, username=self.role, password=self.password, query={})
        self.created = []
        self.role_created = False
        self.audit = {"database": self.name, "restore_database": self.restore_name, "role": self.role,
                      "host": self.url.host, "port": self.url.port or 5432, "run_id": self.run_id}
        self.folder = ROOT / "docs_private" / "p0b_acceptance" / self.run_id

    def engine(self, database=None):
        name = database or self.name
        if name not in self.created:
            raise RuntimeError("Database was not created by this run")
        return create_async_engine(self.url.set(database=name), echo=False, hide_parameters=True,
            connect_args={"server_settings": {"search_path": "public", "application_name": "p0b_acceptance",
                                              "statement_timeout": "15000", "lock_timeout": "5000"}})

    async def assert_target(self, connection, database=None):
        expected = database or self.name
        row = (await connection.execute(text("""SELECT current_database(), current_user,
            shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()"""))).one()
        if row != (expected, self.role, self.marker) or expected not in self.created:
            raise RuntimeError("Isolated database identity check failed")

    async def business_snapshot(self):
        engine = create_async_engine(self.admin_url, echo=False, hide_parameters=True)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                revision = (await conn.execute(text("SELECT version_num FROM public.alembic_version"))).scalar_one()
                columns = (await conn.execute(text("""SELECT column_name FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='trip_place_imports' ORDER BY ordinal_position"""))).scalars().all()
                return {"database": self.admin_url.database, "revision": revision, "receipt_columns": columns}
        finally:
            await engine.dispose()

    async def create_database(self, admin, name):
        if not re.fullmatch(r"grouphang_p0b_(?:test|restore)_[0-9a-f]{16}", name):
            raise RuntimeError("Unsafe test database name")
        if name == self.admin_url.database:
            raise RuntimeError("Test database equals business database")
        await admin.execute(text(f'CREATE DATABASE "{name}" OWNER "{self.role}" TEMPLATE template0'))
        self.created.append(name)
        await admin.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{self.marker}'"))

    async def prepare(self):
        self.audit["business_before"] = await self.business_snapshot()
        admin_engine = create_async_engine(self.admin_url, isolation_level="AUTOCOMMIT", echo=False, hide_parameters=True)
        try:
            async with admin_engine.connect() as admin:
                # Never log or propagate SQL containing the generated credential.
                try:
                    await admin.execute(text(f'CREATE ROLE "{self.role}" LOGIN NOSUPERUSER NOCREATEDB '
                                             f"NOCREATEROLE NOINHERIT PASSWORD '{self.password}'"))
                    self.role_created = True
                except Exception:
                    raise RuntimeError("Could not create isolated test role") from None
                access = (await admin.execute(text("""SELECT count(*) FROM pg_class c JOIN pg_namespace n
                    ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname='public'
                    AND has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')"""),
                    {"role": self.role})).scalar_one()
                if access:
                    raise RuntimeError("Test role has access to business tables; refusing tests")
                self.audit["business_tables_accessible_to_test_role"] = access
                await self.create_database(admin, self.name)
                await self.create_database(admin, self.restore_name)
        finally:
            await admin_engine.dispose()
        for name in self.created:
            engine = self.engine(name)
            try:
                async with engine.connect() as conn:
                    await self.assert_target(conn, name)
                    count = (await conn.execute(text("""SELECT count(*) FROM pg_tables
                        WHERE schemaname NOT IN ('pg_catalog','information_schema')"""))).scalar_one()
                    if count != 0:
                        raise RuntimeError("Fresh test database is not empty")
                    privileges = (await conn.execute(text("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user"))).one()
                    if any(privileges):
                        raise RuntimeError("Test role is not restricted")
            finally:
                await engine.dispose()
        self.audit["initial_user_tables"] = 0
        self.audit["restricted_role"] = True
        await self.migrate("0015_recovery_code")
        await self.seed_legacy()
        await self.backup_restore_drill()
        # DDL + alembic_version must both roll back if the surrounding transaction fails.
        engine = self.engine()
        try:
            try:
                async with engine.begin() as conn:
                    await self.assert_target(conn)
                    await self.run_migration(conn, "upgrade", "0016_text_place_confirmation")
                    raise RuntimeError("synthetic migration transaction failure")
            except RuntimeError as exc:
                if str(exc) != "synthetic migration transaction failure":
                    raise
            async with engine.connect() as conn:
                revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
                new_column = (await conn.execute(text("""SELECT count(*) FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='trip_place_imports' AND column_name='request_hash'"""))).scalar_one()
                if revision != "0015_recovery_code" or new_column:
                    raise RuntimeError("Migration rollback did not restore 0015")
            self.audit["migration_failure_rollback"] = True
        finally:
            await engine.dispose()
        await self.migrate("0016_text_place_confirmation")
        # No text receipts yet: prove the allowed downgrade and re-upgrade on synthetic data.
        await self.migrate("0015_recovery_code", direction="downgrade")
        await self.migrate("0016_text_place_confirmation")
        self.audit["upgrade_downgrade_reupgrade"] = True
        self.folder.mkdir(parents=True, exist_ok=True)
        self.write_audit()

    async def run_migration(self, conn, direction, revision):
        await self.assert_target(conn)
        def execute(sync_conn):
            cfg = Config(str(ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "migrations"))
            cfg.attributes["connection"] = sync_conn
            getattr(command, direction)(cfg, revision)
        await conn.run_sync(execute)

    async def migrate(self, revision, direction="upgrade"):
        engine = self.engine()
        try:
            async with engine.begin() as conn:
                await self.run_migration(conn, direction, revision)
        finally:
            await engine.dispose()

    async def seed_legacy(self):
        engine = self.engine()
        try:
            async with engine.begin() as conn:
                await self.assert_target(conn)
                user = (await conn.execute(text("INSERT INTO users(name) VALUES ('synthetic legacy user') RETURNING id"))).scalar_one()
                trip = (await conn.execute(text("INSERT INTO trips(title,creator_id,invite_code) VALUES ('synthetic legacy trip',:u,'P0BLEG01') RETURNING id"), {"u": user})).scalar_one()
                dest = (await conn.execute(text("""INSERT INTO trip_destinations(trip_id,submitted_by,name,lat,lng)
                    VALUES (:t,:u,'synthetic legacy place',31.2,121.4) RETURNING id"""), {"t": trip, "u": user})).scalar_one()
                key = hashlib.sha256(b"synthetic legacy source").hexdigest()
                await conn.execute(text("""INSERT INTO trip_place_imports(trip_id,user_id,source_key,destination_id)
                    VALUES (:t,:u,:k,:d)"""), {"t": trip, "u": user, "k": key, "d": dest})
                self.audit["legacy_receipt"] = {"trip_id": trip, "user_id": user, "source_key": key, "destination_id": dest}
        finally:
            await engine.dispose()

    async def backup_restore_drill(self):
        binaries = Path(r"C:\Program Files\PostgreSQL\18\bin")
        dump, restore = binaries / "pg_dump.exe", binaries / "pg_restore.exe"
        if not dump.is_file() or not restore.is_file():
            raise RuntimeError("PostgreSQL backup tools are required for this acceptance run")
        self.folder.mkdir(parents=True, exist_ok=True)
        artifact = self.folder / "synthetic_0015.dump"
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
        env["PGPASSWORD"] = self.password
        common = ["--host", self.url.host, "--port", str(self.url.port or 5432), "--username", self.role, "--no-password"]
        for args in [[str(dump), *common, "--format=custom", "--no-owner", "--no-acl", "--file", str(artifact), self.name],
                     [str(restore), *common, "--exit-on-error", "--no-owner", "--no-acl", "--dbname", self.restore_name, str(artifact)]]:
            result = await asyncio.to_thread(subprocess.run, args, env=env, capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError("Synthetic backup/restore tool failed; provider output suppressed")
        engine = self.engine(self.restore_name)
        try:
            async with engine.connect() as conn:
                await self.assert_target(conn, self.restore_name)
                revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
                receipts = (await conn.execute(text("SELECT count(*) FROM trip_place_imports"))).scalar_one()
                if revision != "0015_recovery_code" or receipts != 1:
                    raise RuntimeError("Restored synthetic backup verification failed")
        finally:
            await engine.dispose()
        self.audit["backup_restore_verified"] = True
        self.audit["backup_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.audit["backup_file"] = str(artifact.relative_to(ROOT))

    def write_audit(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "isolation_evidence.json").write_text(json.dumps(self.audit, ensure_ascii=False, indent=2), encoding="utf-8")

    async def cleanup(self):
        engine = create_async_engine(self.admin_url, isolation_level="AUTOCOMMIT", echo=False, hide_parameters=True)
        try:
            async with engine.connect() as conn:
                for name in reversed(self.created):
                    marker = (await conn.execute(text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"), {"name": name})).scalar_one_or_none()
                    if marker != self.marker or not re.fullmatch(r"grouphang_p0b_(?:test|restore)_[0-9a-f]{16}", name):
                        raise RuntimeError("Cleanup refused: test database marker mismatch")
                    await conn.execute(text(f'DROP DATABASE "{name}"'))
                if self.role_created:
                    await conn.execute(text(f'DROP ROLE "{self.role}"'))
        finally:
            await engine.dispose()
        self.audit["cleanup_completed"] = True
        self.audit["business_after"] = await self.business_snapshot()
        self.audit["business_schema_unchanged"] = self.audit.get("business_before") == self.audit["business_after"]
        self.write_audit()
        if not self.audit["business_schema_unchanged"]:
            raise RuntimeError("Business schema snapshot changed during acceptance; review required")
