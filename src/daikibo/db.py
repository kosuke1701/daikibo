"""Single-writer local store and content-addressed blobs."""
from __future__ import annotations
import contextlib
import fcntl
import os
import sqlite3
import threading
from pathlib import Path
from .common import Fault, need, digest, canonical, atomic_write

SCHEMA_VERSION = 16


def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute migration statements without crossing the caller's transaction."""
    statement = ""
    for character in script:
        statement += character
        # Migrations contain both compact one-line statements and trigger
        # bodies with an inner semicolon.  sqlite3.complete_statement keeps
        # the latter together; splitting only at a complete semicolon also
        # avoids executescript's implicit transaction boundary.
        if character == ";" and sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                connection.execute(sql)
            statement = ""
    if statement.strip():
        connection.execute(statement)


def _backfill_program_origins(connection: sqlite3.Connection) -> None:
    """Create schema-migration origins while the v16 migration is open."""
    from .program_origins import migration_body

    programs = [dict(row) for row in connection.execute(
        "SELECT * FROM programs ORDER BY id"
    ).fetchall()]
    for row in programs:
        body = migration_body(row)
        encoded = canonical(body).decode()
        expected_digest = digest(body)
        connection.execute(
            "INSERT INTO program_origins(program,project,digest,body) VALUES(?,?,?,?)",
            (row["id"], row["project"], expected_digest, encoded),
        )
    count = connection.execute("SELECT count(*) FROM program_origins").fetchone()[0]
    need(count == len(programs), "origin_invalid", "Program origin migration is not one-to-one")

class Store:
    def __init__(self, home: str | Path):
        self.home = Path(home).absolute()
        need(not self.home.is_symlink(), "unsafe_home", "Control home must not be a symlink")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lockfile = open(self.home / "owner.lock", "a+b")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lockfile.close()
            raise Fault("already_running", "Another daemon owns this control home") from exc
        self.lock = threading.RLock()
        self.local = threading.local()
        self.conn = sqlite3.connect(self.home / "state.sqlite3", isolation_level=None, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        # One serialized connection: rollback journal avoids unsafe multi-writer WAL assumptions.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA trusted_schema=OFF")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        need(version <= SCHEMA_VERSION, "newer_database", "Use matching or newer program for this database")
        if version == 0:
            script = Path(__file__).with_name("schema.sql").read_text()
            self.conn.executescript("BEGIN IMMEDIATE;\n" + script + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
        elif version < SCHEMA_VERSION:
            previous=self.home/f'pre-migration-v{version}.sqlite3'
            need(not previous.exists(),'migration_backup_exists','Verify and move the previous migration backup before retrying')
            target=sqlite3.connect(previous)
            try:self.conn.backup(target)
            finally:target.close()
            os.chmod(previous,0o600)
            need(self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='program_origins'"
            ).fetchone() is None,
                 "origin_schema_invalid",
                 "Schema 16 program-origin metadata exists before migration")
            migrations={
                2:"CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),body TEXT NOT NULL CHECK(json_valid(body)),status TEXT NOT NULL,created REAL NOT NULL);",
                3:"CREATE TABLE IF NOT EXISTS review_scopes(id TEXT PRIMARY KEY,program TEXT NOT NULL REFERENCES programs(id),project TEXT NOT NULL REFERENCES projects(id),phase TEXT NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),digest TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL); CREATE INDEX IF NOT EXISTS review_scopes_program ON review_scopes(program,phase,status);"
            }
            migrations[4] = '\nCREATE TABLE IF NOT EXISTS native_sessions(id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), client TEXT NOT NULL, cwd TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), updated REAL NOT NULL);\nCREATE INDEX IF NOT EXISTS native_sessions_workspace ON native_sessions(cwd,updated);\nCREATE TABLE IF NOT EXISTS native_turns(session TEXT NOT NULL REFERENCES native_sessions(id), turn_id TEXT NOT NULL, source TEXT NOT NULL REFERENCES sources(id), digest TEXT NOT NULL, origin TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(session,turn_id));\nCREATE INDEX IF NOT EXISTS native_turns_source ON native_turns(session,source);\n'
            migrations[5] = 'CREATE TABLE IF NOT EXISTS job_attempts(job TEXT NOT NULL REFERENCES jobs(id),attempt INTEGER NOT NULL,status TEXT NOT NULL,started REAL NOT NULL,ended REAL,result TEXT,error TEXT,PRIMARY KEY(job,attempt));\nCREATE TABLE IF NOT EXISTS execution_limits(project TEXT PRIMARY KEY REFERENCES projects(id),revision INTEGER NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),reason TEXT NOT NULL,updated REAL NOT NULL);\nCREATE TABLE IF NOT EXISTS execution_usage(run TEXT PRIMARY KEY REFERENCES runs(id),project TEXT NOT NULL REFERENCES projects(id),adapter TEXT NOT NULL,status TEXT NOT NULL,tokens INTEGER,cost_microusd INTEGER,body TEXT NOT NULL CHECK(json_valid(body)),created REAL NOT NULL,updated REAL);\nCREATE INDEX IF NOT EXISTS execution_usage_project ON execution_usage(project,status);\nCREATE TABLE IF NOT EXISTS knowledge_snapshots(baseline TEXT PRIMARY KEY REFERENCES baselines(id),project TEXT NOT NULL REFERENCES projects(id),blob TEXT NOT NULL,git_commit TEXT NOT NULL,created REAL NOT NULL);\n'
            existing_columns = {row[1] for row in self.conn.execute('PRAGMA table_info(jobs)')}
            if 'attempt_count' not in existing_columns:
                migrations[5] += 'ALTER TABLE jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;'
            if 'retry_due' not in existing_columns:
                migrations[5] += 'ALTER TABLE jobs ADD COLUMN retry_due REAL;'
            if 'retry_deadline' not in existing_columns:
                migrations[5] += 'ALTER TABLE jobs ADD COLUMN retry_deadline REAL;'
            if 'retry_policy' not in existing_columns:
                migrations[5] += "ALTER TABLE jobs ADD COLUMN retry_policy TEXT NOT NULL DEFAULT '{}';"
            if 'retry_fingerprint' not in existing_columns:
                migrations[5] += 'ALTER TABLE jobs ADD COLUMN retry_fingerprint TEXT;'
            migrations[6] = "\nCREATE TABLE IF NOT EXISTS breakdowns (\n id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id), project TEXT NOT NULL REFERENCES projects(id),\n body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,\n status TEXT NOT NULL CHECK(status IN ('proposed','active','superseded')), previous TEXT REFERENCES breakdowns(id), created REAL NOT NULL\n);\nCREATE UNIQUE INDEX IF NOT EXISTS breakdowns_active ON breakdowns(program) WHERE status='active';\nCREATE TABLE IF NOT EXISTS breakdown_packets (\n id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL\n);\nCREATE TABLE IF NOT EXISTS breakdown_members (\n breakdown TEXT NOT NULL REFERENCES breakdowns(id), packet TEXT NOT NULL REFERENCES breakdown_packets(id), ordinal INTEGER NOT NULL,\n PRIMARY KEY(breakdown,packet), UNIQUE(breakdown,ordinal)\n);\nCREATE INDEX IF NOT EXISTS breakdown_members_packet ON breakdown_members(packet);\nCREATE TABLE IF NOT EXISTS breakdown_adoptions (\n breakdown TEXT PRIMARY KEY REFERENCES breakdowns(id), body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL\n);\nCREATE TABLE IF NOT EXISTS program_closures (\n id TEXT PRIMARY KEY, program TEXT NOT NULL REFERENCES programs(id), project TEXT NOT NULL REFERENCES projects(id),\n delivery TEXT NOT NULL REFERENCES deliveries(id), binding TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL\n);\nCREATE INDEX IF NOT EXISTS program_closures_program ON program_closures(program,created);\nCREATE TRIGGER IF NOT EXISTS breakdowns_no_rewrite BEFORE UPDATE OF body,digest,program,project,previous ON breakdowns BEGIN SELECT RAISE(ABORT,'immutable breakdown proposal'); END;\nCREATE TRIGGER IF NOT EXISTS breakdown_packets_no_rewrite BEFORE UPDATE ON breakdown_packets BEGIN SELECT RAISE(ABORT,'immutable breakdown packet'); END;\nCREATE TRIGGER IF NOT EXISTS program_closures_no_rewrite BEFORE UPDATE ON program_closures BEGIN SELECT RAISE(ABORT,'immutable workflow closure'); END;\n"
            migrations[6] += '\nCREATE TABLE IF NOT EXISTS supervisor_views (\n seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL REFERENCES projects(id), method TEXT NOT NULL,\n request_digest TEXT NOT NULL, response_digest TEXT NOT NULL, receipt TEXT NOT NULL REFERENCES receipts(id), created REAL NOT NULL,\n UNIQUE(project,method,request_digest,response_digest)\n);\nCREATE INDEX IF NOT EXISTS supervisor_views_project ON supervisor_views(project,seq);\n'
            from .breakdown_inputs import SCHEMA as UPLOAD_SCHEMA
            migrations[7] = UPLOAD_SCHEMA
            from .task_revisions import SCHEMA as TASK_REVISION_SCHEMA
            migrations[8] = TASK_REVISION_SCHEMA
            from .workstreams import SCHEMA as WORKSTREAM_SCHEMA
            migrations[9] = WORKSTREAM_SCHEMA
            from .scope_returns import SCHEMA as SCOPE_RETURN_SCHEMA
            migrations[10] = SCOPE_RETURN_SCHEMA
            from .subplans import SCHEMA as SUBPLAN_SCHEMA
            migrations[11] = SUBPLAN_SCHEMA
            from .local_executions import SCHEMA as LOCAL_EXECUTION_SCHEMA
            migrations[12] = LOCAL_EXECUTION_SCHEMA
            from .execution_controls import SCHEMA as EXECUTION_CONTROL_SCHEMA
            execution_schema = EXECUTION_CONTROL_SCHEMA
            task_columns = {row[1] for row in self.conn.execute('PRAGMA table_info(tasks)')}
            if 'no_progress_count' in task_columns:
                execution_schema = execution_schema.replace(
                    'ALTER TABLE tasks ADD COLUMN no_progress_count INTEGER NOT NULL DEFAULT 0;\n', '', 1)
            migrations[13] = execution_schema
            from .traceability import SCHEMA as TRACEABILITY_SCHEMA
            migrations[14] = TRACEABILITY_SCHEMA
            from .assurance import SCHEMA as ASSURANCE_SCHEMA
            migrations[15] = ASSURANCE_SCHEMA
            migrations[16] = """
CREATE TABLE IF NOT EXISTS program_origins (
 program TEXT PRIMARY KEY REFERENCES programs(id),
 project TEXT NOT NULL REFERENCES projects(id),
 digest TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)),
 UNIQUE(program,project)
);
CREATE INDEX IF NOT EXISTS program_origins_project ON program_origins(project,program);
CREATE TRIGGER IF NOT EXISTS program_origins_no_update BEFORE UPDATE ON program_origins
 BEGIN SELECT RAISE(ABORT,'immutable program origin'); END;
CREATE TRIGGER IF NOT EXISTS program_origins_no_delete BEFORE DELETE ON program_origins
 BEGIN SELECT RAISE(ABORT,'retain program origin history'); END;
"""
            script=''.join(migrations[v] for v in range(version+1,SCHEMA_VERSION+1))
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                _execute_sql_script(self.conn, script)
                _backfill_program_origins(self.conn)
                self.conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        self.blobs = self.home / "blobs"
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        # Backup recipes are private artifact metadata, deliberately separate
        # from ordinary content-addressed leaves.  A recipe is never a blob
        # leaf and is never followed by blob_path().
        self.backup_recipes = self.home / "backup-recipes"
        self.backup_recipes.mkdir(mode=0o700, exist_ok=True)
        self._artifact_manager = None
        self._closed = False

    def _get_artifact_manager(self):
        if self._artifact_manager is None:
            from .backup_artifacts import ArtifactSessionManager
            self._artifact_manager = ArtifactSessionManager(self)
        return self._artifact_manager

    @contextlib.contextmanager
    def transaction(self, *, rollback_blobs=False):
        with self.lock:
            depth = getattr(self.local, "depth", 0)
            journal = getattr(self.local, "blob_journal", None)
            owns_journal = rollback_blobs and journal is None
            if owns_journal:
                journal = []
                self.local.blob_journal = journal
            journal_start = len(journal) if journal is not None else 0
            self.local.depth = depth + 1
            savepoint = f"nested_{depth}"
            self.conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
            try:
                yield self
                self.conn.execute("COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                self.conn.execute("ROLLBACK" if depth == 0 else f"ROLLBACK TO SAVEPOINT {savepoint}")
                if depth:
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if journal is not None:
                    for path in reversed(journal[journal_start:]):
                        path.unlink(missing_ok=True)
                        try: path.parent.rmdir()
                        except OSError: pass
                    del journal[journal_start:]
                raise
            finally:
                if owns_journal:
                    del self.local.blob_journal
                self.local.depth = depth

    def execute(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def one(self, sql: str, args=(), required=False) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            need(row is not None or not required, "not_found", "Requested record does not exist")
            return dict(row) if row is not None else None

    def all(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def blob_put(self, data: bytes) -> str:
        # Blob publication shares the writer lock with rollback journals:
        # another thread cannot adopt an uncommitted newly-created blob.
        with self.lock:
            return self._blob_put(data)

    def _blob_put(self, data: bytes) -> str:
        h = digest(data)
        path = self.blobs / h[:2] / h[2:]
        if path.exists():
            need(digest(path.read_bytes()) == h, "integrity_error", "Existing blob was modified")
        else:
            atomic_write(path, data)
            journal = getattr(self.local, "blob_journal", None)
            if journal is not None:
                journal.append(path)
            if self._artifact_manager is not None:
                self._artifact_manager.bump()
        return h

    def blob_get(self, h: str) -> bytes:
        need(isinstance(h, str) and len(h) == 64 and all(c in "0123456789abcdef" for c in h), "invalid_digest", "Expected SHA-256")
        path = self.blobs / h[:2] / h[2:]
        if path.exists() or path.is_symlink():
            need(path.is_file() and not path.is_symlink(), 'integrity_error', 'Content-addressed blob is not a regular file', h)
            data = path.read_bytes()
            need(digest(data) == h, "integrity_error", "Content-addressed blob was modified", h)
            return data
        recipe = self.backup_recipes / f"{h}.json"
        need(recipe.is_file() and not recipe.is_symlink(), "missing_evidence", "Content-addressed blob is missing", h)
        # Keep the historical blob_get bytes contract while using the bounded
        # artifact reader underneath.  Public range reads use the same reader
        # without this whole-result allocation.
        from .backup_artifacts import open_artifact_session
        value = bytearray()
        with open_artifact_session(self, h) as session:
            total = session.size
            offset = 0
            while offset < total:
                block = session.read_range(offset, min(1024 * 1024, total - offset))
                need(block, "integrity_error", "Recipe artifact ended before its declared size", h)
                value.extend(block)
                offset += len(block)
        data = bytes(value)
        need(digest(data) == h, "integrity_error", "Recipe artifact hash differs", h)
        return data

    def blob_path(self, h: str) -> Path:
        """Locate a blob for streaming; callers verify bytes while consuming it."""
        need(isinstance(h,str) and len(h)==64 and all(ch in '0123456789abcdef' for ch in h),
             'invalid_digest','Expected SHA-256')
        path=self.blobs/h[:2]/h[2:]
        if path.exists() or path.is_symlink():
            need(path.is_file() and not path.is_symlink(), 'integrity_error', 'Content-addressed blob is not a regular file', h)
            return path
        recipe = self.backup_recipes / f'{h}.json'
        if recipe.is_file() and not recipe.is_symlink():
            raise Fault('artifact_requires_stream','Recipe-backed backup artifacts do not have a physical blob path',h)
        need(False,'missing_evidence','Content-addressed blob is absent',h)
        return path

    def blob_put_file(self, source: Path) -> str:
        """Ingest a large file without a second full in-memory copy."""
        flags=os.O_RDONLY | getattr(os,'O_CLOEXEC',0) | getattr(os,'O_NOFOLLOW',0)
        fd=os.open(Path(source),flags)
        try:
            with os.fdopen(fd,'rb',closefd=True) as incoming:
                result,_=self.blob_put_stream(incoming)
            return result
        except BaseException:
            # fdopen owns the descriptor after successful construction; if it
            # fails before that point, close it without masking the original
            # filesystem error.
            try:os.close(fd)
            except OSError:pass
            raise

    def blob_put_stream(self, source, chunk_bytes: int = 1024 * 1024) -> tuple[str, int]:
        """Ingest a binary stream with bounded memory under the writer lock."""
        with self.lock:
            return self._blob_put_stream(source, chunk_bytes)

    def _blob_put_stream(self, source, chunk_bytes: int) -> tuple[str, int]:
        """Ingest an already-open binary stream with bounded memory.

        Failure-retention opens worker files with ``O_NOFOLLOW`` and passes the
        descriptor here.  The stream is owned by the caller; only the
        temporary content-addressed output is closed by this method.
        """
        import hashlib, tempfile
        need(type(chunk_bytes) is int and 1 <= chunk_bytes <= 16 * 1024 * 1024,
             'invalid_range', 'Blob stream chunk size is outside the bounded range')
        fd, temporary = tempfile.mkstemp(prefix='.blob-stream-', dir=self.blobs)
        total = 0
        try:
            hashed = hashlib.sha256()
            with os.fdopen(fd, 'wb') as output:
                while block := source.read(chunk_bytes):
                    need(isinstance(block, (bytes, bytearray)), 'invalid_blob_stream', 'Blob stream returned non-bytes')
                    total += len(block); hashed.update(block); output.write(block)
                output.flush(); os.fsync(output.fileno())
            h = hashed.hexdigest(); target = self.blobs / h[:2] / h[2:]; target.parent.mkdir(exist_ok=True)
            if target.exists():
                with self.blob_path(h).open('rb') as existing:
                    need(hashlib.file_digest(existing, 'sha256').hexdigest() == h,
                         'integrity_error', 'Existing blob differs')
            else:
                os.replace(temporary, target)
                journal = getattr(self.local, "blob_journal", None)
                if journal is not None:
                    journal.append(target)
                if self._artifact_manager is not None:
                    self._artifact_manager.bump()
            directory = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(directory)
            finally: os.close(directory)
            return h, total
        finally:
            Path(temporary).unlink(missing_ok=True)

    def backup_database(self, path: Path):
        with self.lock:
            target = sqlite3.connect(path)
            try:
                self.conn.backup(target)
                need(target.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity_error", "Backup integrity check failed")
            finally:
                target.close()

    def close(self):
        if not self._closed:
            with self.lock:
                if self._artifact_manager is not None:
                    self._artifact_manager.close()
                self.conn.close()
                fcntl.flock(self.lockfile, fcntl.LOCK_UN)
                self.lockfile.close()
                self._closed = True
