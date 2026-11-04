"""
Build inventory.db from the CSV files in ../data.

Each CSV becomes one table named after the file. The one exception: the sales CSV
(the agent expects a table called `sales`). It is detected by its columns
(VendorNo + SalesDollars), so it works even if the file is named final.csv.

Run from the vendor-sql-agent folder:  python build_db_from_csv.py
The big sales file is loaded in chunks, so it takes a few minutes. That is normal.
"""
import os
import sqlite3
import sys

import pandas as pd

DATA_DIR = os.getenv("DATA_DIR", os.path.join("..", "data"))
DB_PATH = "inventory.db"
CHUNK = 200_000

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

csv_files = sorted(f for f in os.listdir(DATA_DIR) if f.lower().endswith(".csv"))
if not csv_files:
    sys.exit(f"No CSV files found in {DATA_DIR}")

con = sqlite3.connect(DB_PATH)
sales_found = False

for fname in csv_files:
    path = os.path.join(DATA_DIR, fname)
    header = pd.read_csv(path, nrows=0).columns.tolist()
    stem = os.path.splitext(fname)[0]

    if "VendorNo" in header and "SalesDollars" in header:
        table = "sales"
        sales_found = True
    else:
        table = stem

    total = 0
    for i, chunk in enumerate(pd.read_csv(path, chunksize=CHUNK)):
        chunk.to_sql(table, con, if_exists="replace" if i == 0 else "append", index=False)
        total += len(chunk)
    print(f"{fname} -> table '{table}': {total:,} rows")

con.commit()
con.close()

if not sales_found:
    print("\nWARNING: no sales table detected (no CSV with VendorNo + SalesDollars columns).")
    print("Tell me the column names of final.csv and I will fix the mapping.")
print(f"\nDone. {DB_PATH} size: {os.path.getsize(DB_PATH) / 1e6:.0f} MB")