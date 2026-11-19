"""Read-only reports, plus the loader that fills the database from the CSVs."""
import csv
from pathlib import Path

from . import service
from .db import transaction


def low_stock(conn):
    """Active items at or below their reorder point, most urgent first."""
    return conn.execute(
        """SELECT i.sku, i.name, i.category, i.unit, i.quantity_on_hand AS on_hand,
                  i.reorder_point, ROUND(i.reorder_point - i.quantity_on_hand, 2) AS short_by,
                  s.name AS supplier
           FROM items i JOIN suppliers s ON s.id = i.supplier_id
           WHERE i.is_active = 1 AND i.quantity_on_hand <= i.reorder_point
           ORDER BY (i.quantity_on_hand - i.reorder_point) * i.unit_cost, i.name""").fetchall()


def inventory_value(conn):
    """Value of stock on hand by category, with a grand total row last."""
    rows = conn.execute(
        """SELECT category, COUNT(*) AS items,
                  ROUND(SUM(quantity_on_hand * unit_cost), 2) AS value
           FROM items GROUP BY category ORDER BY value DESC""").fetchall()
    return [dict(r) for r in rows] + [{
        "category": "TOTAL", "items": sum(r["items"] for r in rows),
        "value": round(sum(r["value"] or 0 for r in rows), 2)}]


def monthly_summary(conn):
    """Revenue vs. food cost and waste, month by month.
    food cost % = (cost of food used + wasted) / revenue."""
    return conn.execute(
        """WITH sales AS (
               SELECT substr(sale_date, 1, 7) AS month, SUM(revenue) AS revenue,
                      SUM(is_open) AS open_days
               FROM daily_sales GROUP BY month),
           mv AS (
               SELECT substr(moved_at, 1, 7) AS month,
                      SUM(CASE WHEN movement_type='receive' THEN quantity*unit_cost ELSE 0 END) AS purchases,
                      SUM(CASE WHEN movement_type='use'     THEN quantity*unit_cost ELSE 0 END) AS used,
                      SUM(CASE WHEN movement_type='waste'   THEN quantity*unit_cost ELSE 0 END) AS waste
               FROM stock_movements GROUP BY month)
           SELECT sales.month, ROUND(sales.revenue, 0) AS revenue, sales.open_days,
                  ROUND(COALESCE(mv.purchases, 0), 0) AS purchases,
                  ROUND(COALESCE(mv.waste, 0), 0) AS waste,
                  ROUND(100.0 * (COALESCE(mv.used, 0) + COALESCE(mv.waste, 0)) / sales.revenue, 1)
                      AS food_cost_pct,
                  ROUND(100.0 * COALESCE(mv.waste, 0) /
                        NULLIF(COALESCE(mv.used, 0) + COALESCE(mv.waste, 0), 0), 1) AS waste_pct
           FROM sales LEFT JOIN mv ON mv.month = sales.month
           ORDER BY sales.month""").fetchall()


def top_waste(conn, since=None, until=None, limit=10):
    """Items that cost the most in waste (valued at movement cost)."""
    return conn.execute(
        """SELECT i.sku, i.name, i.category, ROUND(SUM(m.quantity), 1) AS qty_wasted,
                  ROUND(SUM(m.quantity * m.unit_cost), 2) AS waste_cost
           FROM stock_movements m JOIN items i ON i.id = m.item_id
           WHERE m.movement_type = 'waste' AND m.moved_at >= COALESCE(?, '0000-00-00')
                 AND m.moved_at <= COALESCE(?, '9999-12-31')
           GROUP BY i.id ORDER BY waste_cost DESC LIMIT ?""",
        (since, until, limit)).fetchall()


def _date_or_none(s):
    return s or None


def load_dataset(conn, data_dir, through=None):
    """Fill an empty database from the CSV dataset, in ONE transaction (all or
    nothing).  `through` (YYYY-MM-DD) loads only history up to that date -
    handy for a copy that still has stock on hand to practice on."""
    data_dir = Path(data_dir)
    through = through or "9999-12-31"

    def rows(name):
        with open(data_dir / name, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    with transaction(conn):
        if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
            raise service.InventoryError("Database already has items; use --reset to start over")
        for s in rows("suppliers.csv"):
            sid = service.add_supplier(conn, s["name"], s["phone"], s["payment_terms"],
                                       supplier_id=int(s["id"]))
            if _date_or_none(s["cut_off_on"]) and s["cut_off_on"] <= through:
                service.set_supplier_active(conn, sid, False)
        for i in rows("items.csv"):
            service.add_item(conn, i["sku"], i["name"], i["category"], i["unit"],
                             int(i["supplier_id"]), float(i["unit_cost"]),
                             float(i["reorder_point"]))
            if _date_or_none(i["discontinued_on"]) and i["discontinued_on"] <= through:
                service.update_item(conn, i["sku"], is_active=0)
        n_moves = 0
        for m in rows("stock_movements.csv"):
            if m["moved_at"] > through:
                continue
            service.record_movement(conn, m["sku"], m["movement_type"], float(m["quantity"]),
                                    float(m["unit_cost"]), m["moved_at"], m["note"] or None,
                                    audited=False)
            n_moves += 1
        n_sales = 0
        for d in rows("daily_sales.csv"):
            if d["sale_date"] > through:
                continue
            service.record_sale(conn, d["sale_date"], int(d["covers"]), float(d["revenue"]),
                                d["notes"] or None, bool(int(d["is_open"])), audited=False)
            n_sales += 1
        service.audit(conn, "load_dataset", "database", None, movements=n_moves,
                      sales_days=n_sales, through=None if through == "9999-12-31" else through)
    return {"movements": n_moves, "sales_days": n_sales}
