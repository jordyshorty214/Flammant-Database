"""Generate a synthetic dataset for a struggling restaurant that slowly loses
revenue and closes.

Story: "Lone Oak Smokehouse & Grill" (fictional) opens the dataset healthy in
January 2026 and serves its last meal on Sunday 2026-11-15.  Revenue erodes
because of a competitor, a bad health inspection, losing the pitmaster, price
hikes, and finally staff/menu cuts.  Management keeps ordering from stale
numbers (6 weeks behind), so spoilage and food-cost % climb while sales fall.
Later, cash runs short: orders shrink, stockouts appear, a supplier cuts the
restaurant off, and the remaining stock is written off at closure.

Outputs (written to ./data):
    suppliers.csv, items.csv, daily_sales.csv, stock_movements.csv,
    monthly_summary.csv

Run:  python generate_dataset.py
The random seed is fixed, so the output is identical every run.
"""
import csv
import datetime as dt
import math
import random
from collections import defaultdict
from pathlib import Path

random.seed(2026)

OUT = Path(__file__).parent / "data"
RESTAURANT = "Lone Oak Smokehouse & Grill"
START = dt.date(2026, 1, 5)            # a Monday
WEEKS = 45
END = START + dt.timedelta(days=WEEKS * 7 - 1)   # Sunday 2026-11-15 (last day)
CLOSE_MON_TUE_FROM = dt.date(2026, 8, 10)        # staff cuts: closed Mon/Tue
BASE_COVERS = 140          # average covers per open day at the start
AVG_CHECK = 31.0           # average check per cover at the start ($)
DOW = [0.75, 0.80, 0.90, 1.00, 1.30, 1.45, 0.90]  # Mon..Sun
DOW_NORM = sum(DOW) / 7

# date -> (note, persistent covers multiplier, persistent check multiplier)
EVENTS = {
    dt.date(2026, 2, 9): ("New smokehouse chain opens half a mile away", 0.94, 1.00),
    dt.date(2026, 4, 13): ("Health inspection score of 78 hits local news", 0.90, 1.00),
    dt.date(2026, 5, 25): ("Head pitmaster quits; quality complaints rise", 0.93, 0.98),
    dt.date(2026, 7, 6): ("Menu prices raised about 8% to cover costs", 0.96, 1.08),
    dt.date(2026, 8, 10): ("Monday/Tuesday service cut to save labor", 1.00, 1.00),
    dt.date(2026, 8, 31): ("Seafood supplier cuts off credit; seafood off the menu", 1.00, 1.00),
    dt.date(2026, 10, 5): ("Ribs and weekday lunch dropped from the menu", 0.92, 1.00),
    END: ("Final day of service - restaurant closes permanently", 1.00, 1.00),
}
# one-day bumps: date -> (note, covers multiplier)
HOLIDAYS = {
    dt.date(2026, 2, 14): ("Valentine's Day", 1.25),
    dt.date(2026, 5, 10): ("Mother's Day", 1.25),
    dt.date(2026, 6, 21): ("Father's Day", 1.10),
    dt.date(2026, 7, 4): ("Fourth of July (slow)", 0.80),
}

SUPPLIERS = [  # id, name, phone, terms, cut_off_on
    (1, "Lone Star Produce", "214-555-0101", "Net 7", None),
    (2, "Prairie Meat Co.", "214-555-0102", "Net 14", None),
    (3, "Gulf Coast Seafood", "214-555-0103", "Net 7", dt.date(2026, 8, 31)),
    (4, "Metroplex Dairy & Eggs", "214-555-0104", "Net 14", None),
    (5, "Texas Restaurant Supply", "214-555-0105", "Net 30", None),
    (6, "Dallas Beverage Distributors", "214-555-0106", "Net 14", None),
    (7, "Bluebonnet Bakery", "214-555-0107", "Net 7", None),
]

# sku, name, category, unit, supplier_id, base unit cost, relative weekly use, shelf life (days)
ITEMS = [
    ("PRO-001", "Roma Tomatoes", "Produce", "lb", 1, 1.60, 90, 7),
    ("PRO-002", "Yellow Onions", "Produce", "lb", 1, 0.70, 120, 28),
    ("PRO-003", "Romaine Hearts (24 ct case)", "Produce", "case", 1, 38.00, 4, 7),
    ("PRO-004", "Russet Potatoes", "Produce", "lb", 1, 0.55, 200, 21),
    ("PRO-005", "Jalapenos", "Produce", "lb", 1, 1.90, 25, 10),
    ("PRO-006", "Limes", "Produce", "each", 1, 0.18, 300, 14),
    ("PRO-007", "Avocados", "Produce", "each", 1, 1.10, 120, 5),
    ("PRO-008", "Cilantro", "Produce", "bunch", 1, 0.95, 40, 5),
    ("MEA-001", "Chicken Breast", "Meat", "lb", 2, 2.85, 160, 4),
    ("MEA-002", "Ground Beef 80/20", "Meat", "lb", 2, 4.10, 140, 4),
    ("MEA-003", "Brisket (whole packer)", "Meat", "lb", 2, 3.95, 180, 7),
    ("MEA-004", "Pork Ribs", "Meat", "lb", 2, 3.40, 110, 5),
    ("MEA-005", "Smoked Sausage", "Meat", "lb", 2, 4.60, 60, 10),
    ("MEA-006", "Bacon", "Meat", "lb", 2, 5.20, 45, 14),
    ("SEA-001", "Gulf Shrimp 16/20", "Seafood", "lb", 3, 9.80, 55, 3),
    ("SEA-002", "Catfish Fillets", "Seafood", "lb", 3, 5.90, 45, 3),
    ("DAI-001", "Whole Milk", "Dairy", "gal", 4, 3.60, 20, 7),
    ("DAI-002", "Heavy Cream", "Dairy", "qt", 4, 3.90, 14, 10),
    ("DAI-003", "Shredded Cheddar", "Dairy", "lb", 4, 3.70, 50, 21),
    ("DAI-004", "Butter", "Dairy", "lb", 4, 3.50, 35, 30),
    ("DAI-005", "Eggs", "Dairy", "dozen", 4, 3.10, 40, 14),
    ("DRY-001", "Fryer Oil (35 lb jug)", "Dry Goods", "each", 5, 31.00, 6, 180),
    ("DRY-002", "All-Purpose Flour (50 lb)", "Dry Goods", "bag", 5, 21.00, 3, 180),
    ("DRY-003", "Long Grain Rice (25 lb)", "Dry Goods", "bag", 5, 17.50, 3, 365),
    ("DRY-004", "Pinto Beans", "Dry Goods", "lb", 5, 1.10, 40, 365),
    ("DRY-005", "BBQ Sauce", "Dry Goods", "gal", 5, 12.00, 5, 120),
    ("DRY-006", "Flour Tortillas (36 pk)", "Dry Goods", "pack", 5, 4.20, 30, 14),
    ("BEV-001", "Draft Beer Keg", "Beverage", "keg", 6, 165.00, 2.5, 60),
    ("BEV-002", "Coffee Beans", "Beverage", "lb", 6, 8.50, 12, 90),
    ("BEV-003", "Fountain Soda Syrup Box", "Beverage", "box", 6, 98.00, 1.5, 180),
    ("BAK-001", "Burger Buns", "Bakery", "dozen", 7, 3.40, 45, 4),
    ("BAK-002", "Texas Toast", "Bakery", "loaf", 7, 2.20, 25, 4),
]
DROP = {  # sku -> date the item leaves the menu
    "SEA-001": dt.date(2026, 8, 31),
    "SEA-002": dt.date(2026, 8, 31),
    "MEA-004": dt.date(2026, 10, 5),
}
ORDER = {"receive": 0, "adjust_in": 1, "use": 2, "adjust_out": 3, "waste": 4}


def waste_rate(shelf_days):
    """Share of leftover stock that spoils at week's end."""
    if shelf_days <= 5:
        return 0.50
    if shelf_days <= 7:
        return 0.35
    if shelf_days <= 14:
        return 0.12
    if shelf_days <= 30:
        return 0.03
    return 0.003


def price(base, d):
    """Supplier prices creep up, and jump once suppliers want faster payment."""
    p = base * (1 + 0.004 * (d - START).days / 30.4)
    if d >= dt.date(2026, 8, 31):
        p *= 1.06
    return round(p, 2)


def cash_factor(d):
    """How much of the 'normal' order the owner can afford to place."""
    if d < dt.date(2026, 7, 1):
        return 1.0
    months = d.month - 7
    return max(0.40, 0.95 - 0.13 * months)


def build_sales():
    rows = []
    total_days = (END - START).days
    cmult, kmult, notes = 1.0, 1.0, {}
    for i in range(total_days + 1):
        d = START + dt.timedelta(days=i)
        t = i / total_days
        if d in EVENTS:
            note, c, k = EVENTS[d]
            cmult *= c
            kmult *= k
            notes[d] = note
        idx = 1 - 0.80 * t ** 1.3          # accelerating decline
        is_open = not (d >= CLOSE_MON_TUE_FROM and d.weekday() in (0, 1))
        note = notes.get(d, "")
        holiday = HOLIDAYS.get(d)
        if holiday:
            note = (note + "; " if note else "") + holiday[0]
        if is_open:
            covers = BASE_COVERS * DOW[d.weekday()] / DOW_NORM * idx * cmult
            covers *= random.gauss(1, 0.07) * (holiday[1] if holiday else 1)
            covers = max(8, round(covers))
            check = round(AVG_CHECK * kmult * (1 - 0.10 * t) * random.gauss(1, 0.03), 2)
            revenue = round(covers * check, 2)
        else:
            covers, check, revenue = 0, 0.0, 0.0
        rows.append({"sale_date": d.isoformat(), "day_of_week": d.strftime("%a"),
                     "is_open": int(is_open), "covers": covers, "avg_check": check,
                     "revenue": revenue, "notes": note})
    return rows


def simulate_inventory(sales):
    weekly = [sum(r["revenue"] for r in sales[w * 7:(w + 1) * 7]) for w in range(WEEKS)]
    base_week = sum(weekly[:4]) / 4
    raw_cost = sum(it[5] * it[6] for it in ITEMS)
    scale = 0.30 * base_week / raw_cost      # healthy food cost ~30% of sales
    stock = {it[0]: 0.0 for it in ITEMS}
    moves = []

    def add(d, sku, typ, qty, cost, note=""):
        sign = 1 if typ in ("receive", "adjust_in") else -1
        stock[sku] = round(stock[sku] + sign * qty, 2)
        moves.append((d, ORDER[typ], sku, typ, qty, cost, note))

    for w in range(WEEKS):
        mon = START + dt.timedelta(days=7 * w)
        sat, sun = mon + dt.timedelta(days=5), mon + dt.timedelta(days=6)
        delivery = mon + dt.timedelta(days=1 if mon < CLOSE_MON_TUE_FROM else 2)
        lag_idx = weekly[max(0, w - 6)] / base_week      # management reads old numbers
        cash = cash_factor(mon)
        for sku, name, cat, unit, sup, cost0, use_raw, shelf in ITEMS:
            base_use = use_raw * scale
            dropped = sku in DROP and mon >= DROP[sku]
            cost = price(cost0, delivery)
            for k in range(7):
                d = mon + dt.timedelta(days=k)
                if w == 0 and k == 0:
                    add(START, sku, "receive", math.ceil(base_use * 1.2), price(cost0, START),
                        "Opening inventory")
                elif d == delivery and not dropped:
                    need = base_use * lag_idx * 1.2 * cash - stock[sku]
                    if need > 0.3:
                        add(d, sku, "receive", math.ceil(need), cost, "Weekly order")
                if d == sat:
                    if stock[sku] > 0.5 and random.random() < 0.04:
                        add(d, sku, "adjust_out", round(stock[sku] * random.uniform(0.02, 0.06), 2),
                            cost, "Cycle count variance (short)")
                    elif stock[sku] > 0.5 and random.random() < 0.02:
                        add(d, sku, "adjust_in", round(stock[sku] * random.uniform(0.02, 0.05), 2),
                            cost, "Cycle count variance (over)")
                day_rev = sales[w * 7 + k]["revenue"]
                if day_rev > 0 and not dropped:
                    demand = base_use * (day_rev / base_week) * random.gauss(1, 0.06)
                    used = round(min(demand, stock[sku]), 2)
                    short = demand - used
                    note = ""
                    if short > max(0.5, 0.15 * demand):
                        note = f"Stockout: short {short:.1f} {unit} (unmet demand)"
                    if used > 0:
                        add(d, sku, "use", used, cost, note)
                if d == sun:
                    if w == WEEKS - 1:
                        rate, wnote = 1.0, "Closure write-off"
                    elif dropped:
                        rate, wnote = 1.0, "Menu item discontinued - stock discarded"
                    else:
                        rate, wnote = waste_rate(shelf), "Spoilage"
                    wq = round(stock[sku] * rate, 2)
                    if wq >= 0.05 or (rate == 1.0 and wq > 0):
                        add(d, sku, "waste", wq, cost, wnote)
    moves.sort(key=lambda m: (m[0], m[1], m[2]))
    return moves


def write_csv(name, fieldnames, rows):
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main():
    OUT.mkdir(exist_ok=True)
    sales = build_sales()
    moves = simulate_inventory(sales)
    base_week = sum(r["revenue"] for r in sales[:28]) / 4
    scale = 0.30 * base_week / sum(it[5] * it[6] for it in ITEMS)

    write_csv("suppliers.csv", ["id", "name", "phone", "payment_terms", "cut_off_on"],
              [dict(id=s[0], name=s[1], phone=s[2], payment_terms=s[3],
                    cut_off_on=s[4].isoformat() if s[4] else "") for s in SUPPLIERS])
    write_csv("items.csv", ["sku", "name", "category", "unit", "supplier_id", "unit_cost",
                            "reorder_point", "discontinued_on"],
              [dict(sku=i[0], name=i[1], category=i[2], unit=i[3], supplier_id=i[4],
                    unit_cost=i[5], reorder_point=round(i[6] * scale * 0.5, 1),
                    discontinued_on=DROP[i[0]].isoformat() if i[0] in DROP else "")
               for i in ITEMS])
    write_csv("daily_sales.csv", ["sale_date", "day_of_week", "is_open", "covers",
                                  "avg_check", "revenue", "notes"], sales)
    write_csv("stock_movements.csv", ["moved_at", "sku", "movement_type", "quantity",
                                      "unit_cost", "note"],
              [dict(moved_at=m[0].isoformat(), sku=m[2], movement_type=m[3], quantity=m[4],
                    unit_cost=m[5], note=m[6]) for m in moves])

    month = defaultdict(lambda: defaultdict(float))
    for r in sales:
        m = r["sale_date"][:7]
        month[m]["revenue"] += r["revenue"]
        month[m]["open_days"] += r["is_open"]
    for d, _o, sku, typ, qty, cost, _n in moves:
        if typ == "receive":
            month[d.isoformat()[:7]]["purchases"] += qty * cost
        elif typ == "use":
            month[d.isoformat()[:7]]["used"] += qty * cost
        elif typ == "waste":
            month[d.isoformat()[:7]]["waste"] += qty * cost
    summary = []
    for m in sorted(month):
        v = month[m]
        summary.append({
            "month": m, "open_days": int(v["open_days"]),
            "revenue": round(v["revenue"], 2),
            "avg_daily_revenue": round(v["revenue"] / v["open_days"], 2) if v["open_days"] else 0,
            "food_purchases": round(v["purchases"], 2), "food_used_cost": round(v["used"], 2),
            "waste_cost": round(v["waste"], 2),
            "food_cost_pct": round(100 * (v["used"] + v["waste"]) / v["revenue"], 1),
            "waste_pct": round(100 * v["waste"] / (v["used"] + v["waste"]), 1)})
    write_csv("monthly_summary.csv", list(summary[0]), summary)

    print(f"{RESTAURANT}: {START} to {END}")
    print(f"{len(sales)} sales days, {len(moves)} stock movements, {len(ITEMS)} items")
    print(f"{'month':8}{'revenue':>11}{'food cost %':>13}{'waste %':>9}")
    for s in summary:
        print(f"{s['month']:8}{s['revenue']:>11,.0f}{s['food_cost_pct']:>13}{s['waste_pct']:>9}")


if __name__ == "__main__":
    main()
