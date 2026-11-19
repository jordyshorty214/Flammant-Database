"""Everything that changes data lives here.  Each function is atomic (one
transaction) and writes to the audit log, so the database can never end up
half-updated and every change can be traced."""
import json
import sqlite3
from datetime import date

from .db import transaction

MOVEMENT_SIGN = {"receive": +1, "adjust_in": +1, "use": -1, "waste": -1, "adjust_out": -1}
EDITABLE_ITEM_FIELDS = {"name", "category", "unit", "unit_cost", "reorder_point",
                        "is_active", "supplier_id"}


class InventoryError(Exception):
    """Base class for errors that are safe to show to the user."""


class NotFound(InventoryError):
    pass


class ValidationError(InventoryError):
    pass


class InsufficientStock(InventoryError):
    pass


def audit(conn, action, table, row_id, **detail):
    conn.execute("INSERT INTO audit_log(action, table_name, row_id, detail) VALUES (?,?,?,?)",
                 (action, table, row_id, json.dumps(detail, default=str)))


def _check_date(value):
    if value is None:
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValidationError(f"Invalid date '{value}' (use YYYY-MM-DD)")


# ----------------------------------------------------------------- suppliers
def add_supplier(conn, name, phone=None, payment_terms=None, supplier_id=None):
    if not name or not name.strip():
        raise ValidationError("Supplier name is required")
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO suppliers(id, name, phone, payment_terms) VALUES (?,?,?,?)",
                (supplier_id, name.strip(), phone, payment_terms))
            audit(conn, "add_supplier", "suppliers", cur.lastrowid, name=name)
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValidationError(f"Supplier '{name}' already exists")


def set_supplier_active(conn, supplier_id, active):
    with transaction(conn):
        cur = conn.execute("UPDATE suppliers SET is_active=? WHERE id=?",
                           (1 if active else 0, supplier_id))
        if cur.rowcount == 0:
            raise NotFound(f"No supplier with id {supplier_id}")
        audit(conn, "set_supplier_active", "suppliers", supplier_id, active=bool(active))


def list_suppliers(conn):
    return conn.execute(
        """SELECT s.id, s.name, s.phone, s.payment_terms, s.is_active,
                  COUNT(i.id) AS items
           FROM suppliers s LEFT JOIN items i ON i.supplier_id = s.id
           GROUP BY s.id ORDER BY s.id""").fetchall()


# --------------------------------------------------------------------- items
def get_item(conn, sku):
    row = conn.execute("SELECT * FROM items WHERE sku=?", (sku,)).fetchone()
    if row is None:
        raise NotFound(f"No item with SKU '{sku}'")
    return row


def add_item(conn, sku, name, category, unit, supplier_id, unit_cost, reorder_point=0):
    if not sku or not name:
        raise ValidationError("SKU and name are required")
    if unit_cost < 0 or reorder_point < 0:
        raise ValidationError("Cost and reorder point cannot be negative")
    if conn.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone() is None:
        raise NotFound(f"No supplier with id {supplier_id}")
    try:
        with transaction(conn):
            cur = conn.execute(
                """INSERT INTO items(sku, name, category, unit, supplier_id, unit_cost,
                                     reorder_point) VALUES (?,?,?,?,?,?,?)""",
                (sku.strip(), name.strip(), category, unit, supplier_id, unit_cost, reorder_point))
            audit(conn, "add_item", "items", cur.lastrowid, sku=sku, name=name)
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValidationError(f"SKU '{sku}' already exists")


def update_item(conn, sku, **fields):
    fields = {k: v for k, v in fields.items() if v is not None}
    bad = set(fields) - EDITABLE_ITEM_FIELDS
    if bad:
        raise ValidationError(f"Cannot edit: {', '.join(sorted(bad))}")
    if not fields:
        raise ValidationError("Nothing to update")
    item = get_item(conn, sku)
    try:
        with transaction(conn):
            sets = ", ".join(f"{k}=?" for k in fields)       # keys are whitelisted above
            conn.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item["id"]))
            audit(conn, "update_item", "items", item["id"], sku=sku,
                  before={k: item[k] for k in fields}, after=fields)
    except sqlite3.IntegrityError as e:
        raise ValidationError(f"Update rejected by database rules: {e}")


def remove_item(conn, sku):
    """Delete an item AND its movement history (cascade).  Prefer
    update_item(..., is_active=0) if you only want it off the menu."""
    item = get_item(conn, sku)
    with transaction(conn):
        n = conn.execute("SELECT COUNT(*) FROM stock_movements WHERE item_id=?",
                         (item["id"],)).fetchone()[0]
        conn.execute("DELETE FROM items WHERE id=?", (item["id"],))
        audit(conn, "remove_item", "items", item["id"], sku=sku, name=item["name"],
              movements_deleted=n, last_quantity=item["quantity_on_hand"])


def list_items(conn, include_inactive=False, category=None, search=None):
    sql = """SELECT i.sku, i.name, i.category, i.unit, i.quantity_on_hand, i.reorder_point,
                    i.unit_cost, s.name AS supplier, i.is_active
             FROM items i JOIN suppliers s ON s.id = i.supplier_id WHERE 1=1"""
    args = []
    if not include_inactive:
        sql += " AND i.is_active = 1"
    if category:
        sql += " AND lower(i.category) = lower(?)"
        args.append(category)
    if search:
        sql += " AND (i.name LIKE ? OR i.sku LIKE ?)"
        args += [f"%{search}%", f"%{search}%"]
    return conn.execute(sql + " ORDER BY i.category, i.name", args).fetchall()


# ----------------------------------------------------------------- movements
def record_movement(conn, sku, movement_type, quantity, unit_cost=None, moved_at=None, note=None,
                    audited=True):
    """Receive / use / waste / adjust stock.  Updates the item's on-hand count
    and writes the movement + audit row in ONE transaction.  Stock can never go
    below zero: the whole change is rolled back if it would."""
    if movement_type not in MOVEMENT_SIGN:
        raise ValidationError(f"Unknown movement type '{movement_type}'")
    if quantity is None or quantity <= 0:
        raise ValidationError("Quantity must be greater than zero")
    moved_at = _check_date(moved_at)
    with transaction(conn):
        item = get_item(conn, sku)
        new_qty = round(item["quantity_on_hand"] + MOVEMENT_SIGN[movement_type] * quantity, 4)
        if new_qty < 0:
            raise InsufficientStock(
                f"Only {item['quantity_on_hand']:g} {item['unit']} of {item['name']} on hand; "
                f"cannot remove {quantity:g}")
        cost = item["unit_cost"] if unit_cost is None else unit_cost
        conn.execute("UPDATE items SET quantity_on_hand=? WHERE id=?", (new_qty, item["id"]))
        if movement_type == "receive" and unit_cost is not None:
            conn.execute("UPDATE items SET unit_cost=? WHERE id=?", (unit_cost, item["id"]))
        cur = conn.execute(
            """INSERT INTO stock_movements(item_id, movement_type, quantity, unit_cost,
                                           moved_at, note) VALUES (?,?,?,?,?,?)""",
            (item["id"], movement_type, quantity, cost, moved_at, note))
        if audited:   # bulk imports pass audited=False and log one summary row instead
            audit(conn, f"stock_{movement_type}", "stock_movements", cur.lastrowid, sku=sku,
                  quantity=quantity, before=item["quantity_on_hand"], after=new_qty)
        return cur.lastrowid


def set_count(conn, sku, counted_qty, moved_at=None, note=None):
    """Physical count: records an adjust_in/adjust_out for the difference.
    Returns the signed difference (0 if the count already matches)."""
    if counted_qty < 0:
        raise ValidationError("Counted quantity cannot be negative")
    with transaction(conn):
        item = get_item(conn, sku)
        diff = round(counted_qty - item["quantity_on_hand"], 4)
        if diff == 0:
            return 0
        record_movement(conn, sku, "adjust_in" if diff > 0 else "adjust_out", abs(diff),
                        moved_at=moved_at, note=note or "Physical count correction")
        return diff


def movement_history(conn, sku, limit=25):
    item = get_item(conn, sku)
    return conn.execute(
        """SELECT moved_at, movement_type, quantity, unit_cost, note FROM stock_movements
           WHERE item_id=? ORDER BY moved_at DESC, id DESC LIMIT ?""",
        (item["id"], limit)).fetchall()


def recent_audit(conn, limit=25):
    return conn.execute("SELECT logged_at, action, table_name, row_id, detail FROM audit_log "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# --------------------------------------------------------------------- sales
def record_sale(conn, sale_date, covers, revenue, notes=None, is_open=True, audited=True):
    sale_date = _check_date(sale_date)
    if covers < 0 or revenue < 0:
        raise ValidationError("Covers and revenue cannot be negative")
    avg_check = round(revenue / covers, 2) if covers else 0.0
    with transaction(conn):
        conn.execute(
            """INSERT INTO daily_sales(sale_date, is_open, covers, avg_check, revenue, notes)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(sale_date) DO UPDATE SET is_open=excluded.is_open,
                 covers=excluded.covers, avg_check=excluded.avg_check,
                 revenue=excluded.revenue, notes=excluded.notes""",
            (sale_date, int(is_open), covers, avg_check, revenue, notes))
        if audited:
            audit(conn, "record_sale", "daily_sales", None, sale_date=sale_date,
                  covers=covers, revenue=revenue)
