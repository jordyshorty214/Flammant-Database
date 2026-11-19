"""Command-line interface.  Run:  python -m inventory --help"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

from . import db, reports, service

ROOT = Path(__file__).resolve().parent.parent


def show(rows, columns, empty="(nothing to show)"):
    """Print rows as an aligned text table.  columns = [(header, key, align)]"""
    rows = [dict(r) for r in rows]
    if not rows:
        print(empty)
        return
    def cell(r, key):
        v = r.get(key)
        if v is None:
            return ""
        return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
    widths = [max(len(h), *(len(cell(r, k)) for r in rows)) for h, k, _ in columns]
    def line(vals):
        return "  ".join(v.rjust(w) if a == ">" else v.ljust(w)
                         for v, w, (_, _, a) in zip(vals, widths, columns))
    print(line([h for h, _, _ in columns]))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line([cell(r, k) for _, k, _ in columns]))


# ------------------------------------------------------------------ handlers
def cmd_init(conn, a):
    applied = db.migrate(conn)
    print(f"Schema is at version {db.current_version(conn)}"
          + (f" (applied migrations {applied})" if applied else " (already up to date)"))


def cmd_seed(conn, a):
    if a.reset:
        if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
            print("Saved a safety backup to", db.backup_database(conn, a.backup_dir))
        for t in ("stock_movements", "items", "suppliers", "daily_sales", "audit_log"):
            conn.execute(f"DELETE FROM {t}")
    result = reports.load_dataset(conn, a.data_dir, a.through)
    print(f"Loaded {result['movements']:,} stock movements and {result['sales_days']:,} days of sales.")


def cmd_status(conn, a):
    one = lambda sql: conn.execute(sql).fetchone()[0]
    print(f"Database:        {a.db}")
    print(f"Schema version:  {db.current_version(conn)} of {db.LATEST_VERSION}")
    print(f"Suppliers:       {one('SELECT COUNT(*) FROM suppliers')}")
    print(f"Items:           {one('SELECT COUNT(*) FROM items')} "
          f"({one('SELECT COUNT(*) FROM items WHERE is_active=1')} active)")
    print(f"Movements:       {one('SELECT COUNT(*) FROM stock_movements'):,}  "
          f"({one('SELECT MIN(moved_at) FROM stock_movements')} to "
          f"{one('SELECT MAX(moved_at) FROM stock_movements')})")
    print(f"Sales days:      {one('SELECT COUNT(*) FROM daily_sales'):,}  "
          f"(latest {one('SELECT MAX(sale_date) FROM daily_sales')})")
    print(f"Stock value:     ${one('SELECT COALESCE(SUM(quantity_on_hand*unit_cost),0) FROM items'):,.2f}")
    print(f"Items at/below reorder point: {len(reports.low_stock(conn))}")


def cmd_items(conn, a):
    show(service.list_items(conn, a.all, a.category, a.search),
         [("SKU", "sku", "<"), ("Name", "name", "<"), ("Category", "category", "<"),
          ("Unit", "unit", "<"), ("On hand", "quantity_on_hand", ">"),
          ("Reorder at", "reorder_point", ">"), ("Cost", "unit_cost", ">"),
          ("Supplier", "supplier", "<"), ("Active", "is_active", ">")])


def cmd_add_item(conn, a):
    service.add_item(conn, a.sku, a.name, a.category, a.unit, a.supplier, a.cost, a.reorder_point)
    print(f"Added {a.sku} - {a.name}")


def cmd_edit_item(conn, a):
    service.update_item(conn, a.sku, name=a.name, category=a.category, unit=a.unit,
                        unit_cost=a.cost, reorder_point=a.reorder_point,
                        supplier_id=a.supplier, is_active=a.active)
    print(f"Updated {a.sku}")


def cmd_remove_item(conn, a):
    item = service.get_item(conn, a.sku)
    if not a.yes:
        n = conn.execute("SELECT COUNT(*) FROM stock_movements WHERE item_id=?",
                         (item["id"],)).fetchone()[0]
        reply = input(f"Delete {a.sku} ({item['name']}) and its {n:,} movements? "
                      "This cannot be undone (but you can restore a backup). Type 'yes': ")
        if reply.strip().lower() != "yes":
            print("Cancelled.")
            return
    service.remove_item(conn, a.sku)
    print(f"Removed {a.sku}")


def cmd_suppliers(conn, a):
    show(service.list_suppliers(conn),
         [("ID", "id", ">"), ("Name", "name", "<"), ("Phone", "phone", "<"),
          ("Terms", "payment_terms", "<"), ("Items", "items", ">"), ("Active", "is_active", ">")])


def cmd_add_supplier(conn, a):
    sid = service.add_supplier(conn, a.name, a.phone, a.terms)
    print(f"Added supplier #{sid}: {a.name}")


def cmd_supplier_active(conn, a):
    service.set_supplier_active(conn, a.id, a.state == "on")
    print(f"Supplier #{a.id} is now {'active' if a.state == 'on' else 'inactive'}")


def _move(kind):
    def handler(conn, a):
        service.record_movement(conn, a.sku, kind, a.qty, getattr(a, "cost", None), a.date, a.note)
        item = service.get_item(conn, a.sku)
        print(f"{kind}: {a.qty:g} {item['unit']} of {item['name']}. "
              f"On hand now: {item['quantity_on_hand']:g}")
    return handler


def cmd_count(conn, a):
    diff = service.set_count(conn, a.sku, a.counted, a.date, a.note)
    print("Count matches the system; nothing changed." if diff == 0
          else f"Adjusted by {diff:+g}. On hand now: {a.counted:g}")


def cmd_history(conn, a):
    show(service.movement_history(conn, a.sku, a.limit),
         [("Date", "moved_at", "<"), ("Type", "movement_type", "<"), ("Qty", "quantity", ">"),
          ("Unit cost", "unit_cost", ">"), ("Note", "note", "<")])


def cmd_audit(conn, a):
    show(service.recent_audit(conn, a.limit),
         [("When (UTC)", "logged_at", "<"), ("Action", "action", "<"),
          ("Table", "table_name", "<"), ("Row", "row_id", ">"), ("Detail", "detail", "<")])


def cmd_sale(conn, a):
    service.record_sale(conn, a.date, a.covers, a.revenue, a.notes, is_open=not a.closed)
    print("Saved sales for", a.date)


def cmd_low_stock(conn, a):
    show(reports.low_stock(conn),
         [("SKU", "sku", "<"), ("Name", "name", "<"), ("Unit", "unit", "<"),
          ("On hand", "on_hand", ">"), ("Reorder at", "reorder_point", ">"),
          ("Short by", "short_by", ">"), ("Supplier", "supplier", "<")],
         empty="Nothing is at or below its reorder point.")


def cmd_value(conn, a):
    show(reports.inventory_value(conn),
         [("Category", "category", "<"), ("Items", "items", ">"), ("Value ($)", "value", ">")])


def cmd_monthly(conn, a):
    show(reports.monthly_summary(conn),
         [("Month", "month", "<"), ("Revenue", "revenue", ">"), ("Open days", "open_days", ">"),
          ("Purchases", "purchases", ">"), ("Waste $", "waste", ">"),
          ("Food cost %", "food_cost_pct", ">"), ("Waste %", "waste_pct", ">")])


def cmd_waste_report(conn, a):
    show(reports.top_waste(conn, a.since, a.until, a.limit),
         [("SKU", "sku", "<"), ("Name", "name", "<"), ("Category", "category", "<"),
          ("Qty wasted", "qty_wasted", ">"), ("Waste $", "waste_cost", ">")])


def cmd_backup(conn, a):
    print("Backup saved to", db.backup_database(conn, a.backup_dir))


def cmd_restore(conn, a):
    conn.close()
    safety = db.restore_database(a.file, a.db, a.backup_dir)
    print(f"Restored {a.db} from {a.file}")
    if safety:
        print(f"(Your previous database was saved to {safety})")


# ---------------------------------------------------------------- the parser
def build_parser():
    p = argparse.ArgumentParser(prog="python -m inventory",
                                description="Restaurant inventory database")
    p.add_argument("--db", default=os.environ.get("INVENTORY_DB", db.DEFAULT_DB),
                   help="database file (default: restaurant_inventory.db, or $INVENTORY_DB)")
    p.add_argument("--backup-dir", default="backups", help="where backups are written")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(name, fn, help, args=()):
        sp = sub.add_parser(name, help=help)
        for flags, kw in args:
            sp.add_argument(*flags if isinstance(flags, tuple) else (flags,), **kw)
        sp.set_defaults(fn=fn)
        return sp

    A = lambda *f, **kw: (f, kw)
    add("init", cmd_init, "create/upgrade the database schema")
    add("seed", cmd_seed, "load the restaurant dataset from data/*.csv", [
        A("--data-dir", default=str(ROOT / "data")),
        A("--through", help="only load history up to this date (YYYY-MM-DD)"),
        A("--reset", action="store_true", help="wipe existing data first (backs up first)")])
    add("status", cmd_status, "overview of the database")

    add("items", cmd_items, "list items", [
        A("--all", action="store_true", help="include inactive items"),
        A("--category"), A("--search")])
    add("add-item", cmd_add_item, "add a new item", [
        A("sku"), A("name"), A("--category", required=True), A("--unit", required=True),
        A("--supplier", type=int, required=True, help="supplier id"),
        A("--cost", type=float, required=True), A("--reorder-point", type=float, default=0)])
    add("edit-item", cmd_edit_item, "change an item's details", [
        A("sku"), A("--name"), A("--category"), A("--unit"), A("--cost", type=float),
        A("--reorder-point", type=float), A("--supplier", type=int),
        A("--active", type=int, choices=[0, 1])])
    add("remove-item", cmd_remove_item, "delete an item and its history", [
        A("sku"), A("--yes", action="store_true", help="skip the confirmation prompt")])

    add("suppliers", cmd_suppliers, "list suppliers")
    add("add-supplier", cmd_add_supplier, "add a supplier", [
        A("name"), A("--phone"), A("--terms")])
    add("supplier-active", cmd_supplier_active, "turn a supplier on/off", [
        A("id", type=int), A("state", choices=["on", "off"])])

    for kind, helptext, extra in [
            ("receive", "record a delivery", [A("--cost", type=float, help="unit cost on invoice")]),
            ("use", "record stock used in the kitchen", []),
            ("waste", "record spoiled/discarded stock", [])]:
        add(kind, _move(kind), helptext, [A("sku"), A("qty", type=float),
            A("--date", help="YYYY-MM-DD (default today)"), A("--note"), *extra])
    add("count", cmd_count, "set stock to a physical count (records the difference)", [
        A("sku"), A("counted", type=float), A("--date"), A("--note")])
    add("history", cmd_history, "movement history for one item", [
        A("sku"), A("--limit", type=int, default=25)])
    add("audit", cmd_audit, "recent changes (audit log)", [A("--limit", type=int, default=25)])
    add("sale", cmd_sale, "record a day's sales", [
        A("date"), A("covers", type=int), A("revenue", type=float), A("--notes"),
        A("--closed", action="store_true", help="restaurant was closed that day")])

    add("low-stock", cmd_low_stock, "items at or below their reorder point")
    add("value", cmd_value, "value of stock on hand by category")
    add("monthly", cmd_monthly, "revenue vs food cost and waste by month")
    add("waste-report", cmd_waste_report, "biggest sources of waste", [
        A("--since"), A("--until"), A("--limit", type=int, default=10)])
    add("backup", cmd_backup, "save a backup copy of the database")
    add("restore", cmd_restore, "restore the database from a backup file", [A("file")])
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        conn = db.open_db(args.db)
        args.fn(conn, args)
    except (service.InventoryError, sqlite3.Error, FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
