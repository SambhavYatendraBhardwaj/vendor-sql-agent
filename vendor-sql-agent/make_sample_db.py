"""
Build a small demo database from the full inventory.db (15.6M+ rows, ~2GB).

- final_summary, purchase_prices, vendor_invoice are copied in full (small tables).
- begin_inventory, end_inventory, purchases, sales are evenly down-sampled to ~N rows.

Usage:  python make_sample_db.py            (reads inventory.db, writes sample_inventory.db)
NOTE: totals computed from the sampled raw tables will NOT match the full database.
"""
import os
import sqlite3

SRC = os.getenv("SRC_DB", "inventory.db")
DST = os.getenv("DST_DB", "sample_inventory.db")
N = int(os.getenv("SAMPLE_ROWS", "20000"))
FULL = ["final_summary", "purchase_prices", "vendor_invoice"]
SAMPLED = ["begin_inventory", "end_inventory", "purchases", "sales"]

if os.path.exists(DST):
    os.remove(DST)

con = sqlite3.connect(f"file:{DST}?mode=rwc", uri=True)
con.execute("ATTACH DATABASE ? AS src", (f"file:{SRC}?mode=ro",))
for name in FULL + SAMPLED:
    ddl = con.execute("SELECT sql FROM src.sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    if not ddl:
        print(f"skip {name}: not found")
        continue
    con.execute(ddl[0])
    if name in FULL:
        con.execute(f'INSERT INTO main."{name}" SELECT * FROM src."{name}"')
    else:
        total = con.execute(f'SELECT COUNT(*) FROM src."{name}"').fetchone()[0]
        step = max(total // N, 1)
        con.execute(f'INSERT INTO main."{name}" SELECT * FROM src."{name}" WHERE rowid % ? = 0', (step,))
    con.commit()
    n = con.execute(f'SELECT COUNT(*) FROM main."{name}"').fetchone()[0]
    print(f"{name}: {n:,} rows")
con.execute("DETACH DATABASE src")
con.execute("VACUUM")
con.close()
print(f"Wrote {DST} ({os.path.getsize(DST) / 1e6:.1f} MB)")
