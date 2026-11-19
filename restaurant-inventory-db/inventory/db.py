"""Connection handling, atomic transactions, versioned migrations, backups."""
import itertools
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DEFAULT_DB = "restaurant_inventory.db"

# ---------------------------------------------------------------------------
# Migrations.  NEVER edit an applied migration; add a new one at the end.
# Each migration runs inside one transaction, so it fully applies or not at all.
# ---------------------------------------------------------------------------
MIGRATIONS = [
    (1, "create core tables", """
CREATE TABLE suppliers (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    phone         TEXT,
    payment_terms TEXT
);
CREATE TABLE items (
    id               INTEGER PRIMARY KEY,
    sku              TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,
    unit             TEXT NOT NULL,
    supplier_id      INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    unit_cost        REAL NOT NULL CHECK (unit_cost >= 0),
    quantity_on_hand REAL NOT NULL DEFAULT 0 CHECK (quantity_on_hand >= 0)
);
CREATE TABLE stock_movements (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    movement_type TEXT NOT NULL CHECK (movement_type IN
                  ('receive', 'use', 'waste', 'adjust_in', 'adjust_out')),
    quantity      REAL NOT NULL CHECK (quantity > 0),
    unit_cost     REAL NOT NULL CHECK (unit_cost >= 0),
    moved_at      TEXT NOT NULL,
    note          TEXT
);
CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY,
    logged_at  TEXT NOT NULL DEFAULT (datetime('now')),
    action     TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_id     INTEGER,
    detail     TEXT
);
"""),
    (2, "add reorder points and active flags", """
ALTER TABLE suppliers ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1));
ALTER TABLE items ADD COLUMN reorder_point REAL NOT NULL DEFAULT 0 CHECK (reorder_point >= 0);
ALTER TABLE items ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1));
"""),
    (3, "add indexes for common lookups", """
CREATE INDEX idx_movements_item_date ON stock_movements(item_id, moved_at);
CREATE INDEX idx_movements_type_date ON stock_movements(movement_type, moved_at);
CREATE INDEX idx_items_supplier ON items(supplier_id);
CREATE INDEX idx_audit_logged_at ON audit_log(logged_at);
"""),
    (4, "add daily sales table", """
CREATE TABLE daily_sales (
    sale_date TEXT PRIMARY KEY,
    is_open   INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0, 1)),
    covers    INTEGER NOT NULL DEFAULT 0 CHECK (covers >= 0),
    avg_check REAL NOT NULL DEFAULT 0 CHECK (avg_check >= 0),
    revenue   REAL NOT NULL DEFAULT 0 CHECK (revenue >= 0),
    notes     TEXT
);
"""),
]
LATEST_VERSION = MIGRATIONS[-1][0]


def connect(path=DEFAULT_DB):
    """Open a connection with foreign keys ON and manual transaction control."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_savepoint_ids = itertools.count(1)


@contextmanager
def transaction(conn):
    """Atomic block.  Commits on success, rolls back on ANY exception.
    Nested use becomes a SAVEPOINT, so a failing inner step undoes only itself
    (and an exception that escapes the outer block undoes everything)."""
    if conn.in_transaction:
        name = f"sp_{next(_savepoint_ids)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def current_version(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
    if not exists:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def migrate(conn, target=None):
    """Apply pending migrations up to `target` (default: latest).  Safe to re-run.
    Returns the list of versions applied this call."""
    target = LATEST_VERSION if target is None else target
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    applied = []
    have = current_version(conn)
    for version, name, sql in MIGRATIONS:
        if version <= have or version > target:
            continue
        with transaction(conn):
            for stmt in sql.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                         (version, name))
        applied.append(version)
    return applied


def open_db(path=DEFAULT_DB):
    """Connect and bring the schema fully up to date."""
    conn = connect(path)
    migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------
def backup_database(conn, directory="backups"):
    """Write a consistent copy of the live database; returns the new file's Path."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = Path(directory) / f"inventory_backup_{stamp}.db"
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def restore_database(backup_path, db_path=DEFAULT_DB, safety_dir="backups"):
    """Replace db_path with a backup.  The backup is verified first, and the
    current database is saved to safety_dir before being overwritten."""
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"No such backup: {backup_path}")
    check = sqlite3.connect(str(backup_path))
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        has_schema = check.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
    finally:
        check.close()
    if not ok or not has_schema:
        raise ValueError(f"{backup_path} is not a valid inventory backup")
    safety = None
    if Path(db_path).exists():
        live = connect(db_path)
        try:
            safety = backup_database(live, safety_dir)
        finally:
            live.close()
    shutil.copyfile(backup_path, db_path)
    return safety
