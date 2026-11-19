"""Tests for the restaurant inventory database.

Run with either:
    python -m unittest discover -s tests -v
    pytest -v
"""
import csv
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventory import db, reports, service  # noqa: E402
from inventory.db import transaction  # noqa: E402

DATA = ROOT / "data"


class TempDbCase(unittest.TestCase):
    """Gives every test a fresh, fully-migrated database in a temp folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "test.db"
        self.conn = db.open_db(self.path)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.conn.close)

    def make_item(self, sku="TST-001", qty=0, reorder=0, supplier_name="Test Supplier"):
        row = self.conn.execute("SELECT id FROM suppliers WHERE name=?", (supplier_name,)).fetchone()
        sid = row["id"] if row else service.add_supplier(self.conn, supplier_name)
        service.add_item(self.conn, sku, f"Item {sku}", "Produce", "lb", sid, 2.0, reorder)
        if qty:
            service.record_movement(self.conn, sku, "receive", qty)
        return sku

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class MigrationTests(unittest.TestCase):
    def test_migrations_apply_in_order_and_are_idempotent(self):
        conn = db.connect(":memory:")
        self.assertEqual(db.migrate(conn), [1, 2, 3, 4])
        self.assertEqual(db.migrate(conn), [])          # second run does nothing
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        self.assertEqual(versions, [1, 2, 3, 4])

    def test_migration_adds_columns_and_indexes_without_losing_data(self):
        conn = db.connect(":memory:")
        db.migrate(conn, target=1)                       # an "old" database
        conn.execute("INSERT INTO suppliers(name) VALUES ('Old Supplier')")
        conn.execute("""INSERT INTO items(sku, name, category, unit, supplier_id, unit_cost,
                        quantity_on_hand) VALUES ('OLD-1', 'Old Item', 'Dry', 'lb', 1, 1.5, 7)""")
        db.migrate(conn)                                 # upgrade in place
        row = conn.execute("SELECT * FROM items WHERE sku='OLD-1'").fetchone()
        self.assertEqual(row["quantity_on_hand"], 7)     # data survived
        self.assertEqual(row["reorder_point"], 0)        # new column got its default
        self.assertEqual(row["is_active"], 1)
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
        self.assertIn("idx_movements_item_date", indexes)


class ConstraintTests(TempDbCase):
    def test_duplicate_sku_is_rejected(self):
        self.make_item("DUP-1")
        with self.assertRaises(service.ValidationError):
            self.make_item("DUP-1")
        self.assertEqual(self.count("items"), 1)

    def test_database_itself_refuses_negative_stock(self):
        self.make_item("NEG-1", qty=5)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE items SET quantity_on_hand = -1 WHERE sku='NEG-1'")

    def test_item_must_have_a_real_supplier(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("""INSERT INTO items(sku, name, category, unit, supplier_id, unit_cost)
                                 VALUES ('X', 'X', 'X', 'lb', 999, 1)""")

    def test_supplier_with_items_cannot_be_deleted(self):
        self.make_item("SUP-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM suppliers")
        self.assertEqual(self.count("suppliers"), 1)

    def test_deleting_an_item_cascades_to_its_movements(self):
        self.make_item("CAS-1", qty=10)
        service.record_movement(self.conn, "CAS-1", "use", 3)
        self.assertEqual(self.count("stock_movements"), 2)
        service.remove_item(self.conn, "CAS-1")
        self.assertEqual(self.count("stock_movements"), 0)
        self.assertEqual(self.count("items"), 0)


class ServiceTests(TempDbCase):
    def test_receive_and_use_update_stock_and_latest_cost(self):
        self.make_item("MOV-1")
        service.record_movement(self.conn, "MOV-1", "receive", 50, unit_cost=2.75)
        service.record_movement(self.conn, "MOV-1", "use", 12.5)
        service.record_movement(self.conn, "MOV-1", "waste", 2.5)
        item = service.get_item(self.conn, "MOV-1")
        self.assertEqual(item["quantity_on_hand"], 35)
        self.assertEqual(item["unit_cost"], 2.75)       # receive updates the latest cost

    def test_insufficient_stock_rolls_back_everything(self):
        self.make_item("RB-1", qty=5)
        movements, audits = self.count("stock_movements"), self.count("audit_log")
        with self.assertRaises(service.InsufficientStock):
            service.record_movement(self.conn, "RB-1", "use", 6)
        self.assertEqual(service.get_item(self.conn, "RB-1")["quantity_on_hand"], 5)
        self.assertEqual(self.count("stock_movements"), movements)
        self.assertEqual(self.count("audit_log"), audits)

    def test_failure_inside_a_transaction_undoes_earlier_steps(self):
        self.make_item("TX-1", qty=10)
        with self.assertRaises(service.InsufficientStock):
            with transaction(self.conn):
                service.record_movement(self.conn, "TX-1", "use", 4)     # succeeds...
                service.record_movement(self.conn, "TX-1", "use", 100)   # ...then this fails
        self.assertEqual(service.get_item(self.conn, "TX-1")["quantity_on_hand"], 10)

    def test_physical_count_records_the_difference(self):
        self.make_item("CNT-1", qty=20)
        self.assertEqual(service.set_count(self.conn, "CNT-1", 17.5), -2.5)
        self.assertEqual(service.set_count(self.conn, "CNT-1", 17.5), 0)   # already matches
        last = service.movement_history(self.conn, "CNT-1", 1)[0]
        self.assertEqual((last["movement_type"], last["quantity"]), ("adjust_out", 2.5))

    def test_audit_log_records_changes(self):
        self.make_item("AUD-1", qty=5)
        service.update_item(self.conn, "AUD-1", reorder_point=3)
        actions = [r["action"] for r in service.recent_audit(self.conn, 10)]
        for expected in ("add_supplier", "add_item", "stock_receive", "update_item"):
            self.assertIn(expected, actions)

    def test_low_stock_report_ignores_inactive_and_well_stocked_items(self):
        self.make_item("LOW-1", qty=2, reorder=5)       # low
        self.make_item("OK-1", qty=50, reorder=5)       # fine
        self.make_item("OFF-1", qty=1, reorder=5)       # low but discontinued
        service.update_item(self.conn, "OFF-1", is_active=0)
        self.assertEqual([r["sku"] for r in reports.low_stock(self.conn)], ["LOW-1"])

    def test_backup_is_a_usable_copy_and_restore_recovers_deleted_data(self):
        self.make_item("BK-1", qty=9)
        backup = db.backup_database(self.conn, self.tmp / "bk")
        service.remove_item(self.conn, "BK-1")
        self.conn.close()
        db.restore_database(backup, self.path, self.tmp / "safety")
        restored = db.connect(self.path)
        self.addCleanup(restored.close)
        self.assertEqual(service.get_item(restored, "BK-1")["quantity_on_hand"], 9)


class DatasetTests(unittest.TestCase):
    """Loads the real generated dataset (run generate_dataset.py first)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.conn = db.open_db(cls.tmp / "full.db")
        cls.result = reports.load_dataset(cls.conn, DATA)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, True)

    def test_every_csv_row_is_loaded(self):
        with open(DATA / "stock_movements.csv", newline="") as f:
            n_moves = sum(1 for _ in csv.DictReader(f))
        self.assertEqual(self.result["movements"], n_moves)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], n_moves)

    def test_restaurant_closes_with_nothing_on_hand_and_clean_integrity(self):
        left = self.conn.execute("SELECT COUNT(*) FROM items WHERE quantity_on_hand > 0").fetchone()[0]
        self.assertEqual(left, 0)
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_stored_stock_always_matches_movement_history(self):
        mismatches = self.conn.execute(
            """SELECT i.sku FROM items i WHERE ABS(i.quantity_on_hand - COALESCE((
                   SELECT SUM(CASE WHEN movement_type IN ('receive','adjust_in') THEN quantity
                                   ELSE -quantity END) FROM stock_movements WHERE item_id=i.id), 0)) > 0.001"""
        ).fetchall()
        self.assertEqual(mismatches, [])

    def test_revenue_declines_steadily_to_closure(self):
        months = reports.monthly_summary(self.conn)
        rev = [m["revenue"] for m in months]
        self.assertGreater(rev[0], 100_000)
        self.assertLess(rev[-1], 0.2 * rev[0])
        first_half, second_half = sum(rev[:9]), sum(rev[9:])
        self.assertGreater(first_half, 2 * second_half)

    def test_through_date_gives_open_restaurant_with_stock_and_active_supplier(self):
        conn = db.open_db(self.tmp / "partial.db")
        self.addCleanup(conn.close)
        reports.load_dataset(conn, DATA, through="2025-12-31")
        self.assertGreater(conn.execute("SELECT SUM(quantity_on_hand) FROM items").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT is_active FROM suppliers WHERE id=3").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT MAX(sale_date) FROM daily_sales").fetchone()[0], "2025-12-31")

    def test_loading_into_a_non_empty_database_is_refused(self):
        with self.assertRaises(service.InventoryError):
            reports.load_dataset(self.conn, DATA)


if __name__ == "__main__":
    unittest.main()
