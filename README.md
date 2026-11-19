# Restaurant Inventory Database

A SQLite inventory system built from a synthetic dataset about **Lone Oak
Smokehouse & Grill**, a fictional restaurant that slowly loses revenue and
closes on 2026-06-28. Written and tested on Python 3.12, standard library only (pytest optional).

## The story in the data

| Period | What happens |
|---|---|
| Jan 2026 | Healthy: about $109K/month, food cost about 32% of sales |
| Apr 2026 | A smokehouse chain opens half a mile away |
| Sep 2026 | Health inspection score of 78 makes local news |
| Nov 2026 | Head pitmaster quits; quality complaints rise |

Management orders from numbers that are six weeks stale, so waste climbs as
sales fall. Reorder points are still set for the original volume, so after
the decline nearly everything shows as "low stock". That is part of the story.

## Quick start

```bash
python generate_dataset.py          # (re)creates data/*.csv  - deterministic
python -m inventory seed            # builds restaurant_inventory.db from the CSVs
python -m inventory status
python -m unittest discover -s tests -v     # or: pytest -v
```

`python -m inventory --help` lists everything. Use `--db other.db` or set
`INVENTORY_DB` to work on a different database file.

**Want a restaurant that is still open, with stock on hand?** Load only part
of the history into a practice copy:

```bash
python -m inventory --db practice.db seed --through 2026-03-31
```

## Day-to-day commands

| Task | Command |
|---|---|
| See items | `items` / `items --category Meat` / `items --search brisket` / `items --all` |
| Add an item | `add-item NEW-001 "Pulled Pork" --category Meat --unit lb --supplier 2 --cost 3.75 --reorder-point 40` |
| Edit an item | `edit-item MEA-003 --cost 4.10 --reorder-point 60` (also `--active 0` to retire it) |
| Delivery arrives | `receive MEA-003 120 --cost 4.40 --note "Tuesday order"` |
| Kitchen used stock | `use PRO-001 15` |
| Spoiled / discarded | `waste DAI-001 2 --note "Expired"` |
| Physical count | `count PRO-001 10` (records only the difference) |
| Record a day's sales | `sale 2026-04-02 85 2650.00 --notes "Rainy"` (`--closed` for closed days) |
| What to reorder | `low-stock` |
| One item's history | `history MEA-003 --limit 10` |
| Who changed what | `audit --limit 20` |
| Suppliers | `suppliers` / `add-supplier "Name" --phone 214-555-0199` / `supplier-active 3 off` |
| Stock value | `value` |
| Revenue vs food cost | `monthly` |
| Biggest waste | `waste-report --since 2026-09-01 --limit 10` |
| Delete an item | `remove-item BAK-002` (asks first; removes history too; prefer `--active 0`) |
| Backup / restore | `backup` / `restore backups/<file>.db` |

Dates default to today. Take a `backup` before anything you might regret;
`restore` saves your current database first, so it is reversible too.

## How it is built

```
inventory/db.py        connection, transactions, migrations, backup/restore
inventory/service.py   every data change (atomic + audited)
inventory/reports.py   read-only reports and the CSV loader
inventory/cli.py       command-line interface
generate_dataset.py    creates the dataset
tests/                 20 tests
```

**Tables:** `suppliers`, `items`, `stock_movements`, `daily_sales`,
`audit_log`, `schema_migrations`.

**Data-quality rules enforced by the database itself:** unique SKUs and
supplier names, foreign keys, `quantity_on_hand >= 0`, positive movement
quantities, valid movement types, non-negative costs. Deleting a supplier that
still has items is blocked; deleting an item cascades to its movements.

**Atomic transactions:** each operation (stock change, movement row, audit row)
commits together or not at all. Over-withdrawing stock fails and leaves no trace.

**Versioned migrations:** schema changes live in `MIGRATIONS` in `db.py`.
Version 1 creates the core tables; 2 adds reorder points and active flags with
`ALTER TABLE`; 3 adds indexes; 4 adds daily sales. Existing data is preserved
when upgrading. To change the schema, append a new migration. **Never edit an
old one.**

**Movement types:** `receive` and `adjust_in` add stock; `use`, `waste` and
`adjust_out` remove it. Quantities are always positive; the type sets the direction.

## Ideas to extend it

- A purchase-orders table (migration 5) so `low-stock` can suggest order quantities
- Recipes linking menu items to ingredients, so usage is derived from sales
- A weeks-of-supply report: on hand divided by average weekly use
- Export reports to CSV for a dashboard (Tableau, Excel)
