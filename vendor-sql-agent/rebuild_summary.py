"""
Rebuild final_summary in inventory.db from the raw tables, without join fan-out.

Why: the old final_summary had purchase dollars ~450x too high (146B vs 321.9M raw)
and freight ~450x too high (748.8M vs 1.64M raw), because tables were joined before
being aggregated. Here every raw table is aggregated FIRST, then joined on
(VendorNumber, Brand), so each number is counted once.

Design choices (change them if you disagree):
- Freight is per vendor in vendor_invoice, so it is split across that vendor's brands in
  proportion to purchase dollars. Per-vendor freight totals then match the raw data.
- VendorName and Description are included, so questions about names never need a join.
- Profitmargin is in percent (GrossProfit / TotalSalesDollars * 100).

The old table is only replaced if the new totals match the raw tables (within 2%).

Run from the vendor-sql-agent folder:  python rebuild_summary.py
Takes a few minutes: it scans the 12.8M-row sales table once.
"""
import os
import sqlite3
import sys
import time

DB = os.getenv("SRC_DB", "inventory.db")
TOLERANCE = 0.02

con = sqlite3.connect(DB)
cur = con.cursor()
t0 = time.time()


def log(msg):
    print(f"[{time.time() - t0:6.0f}s] {msg}", flush=True)


old = cur.execute(
    "SELECT COUNT(*), SUM(TotalSalesDollars), SUM(TotalPurchaseDollars), SUM(TotalFreightCost) FROM final_summary"
).fetchone()
log(f"OLD final_summary: rows={old[0]:,} sales={old[1]:,.0f} purchases={old[2]:,.0f} freight={old[3]:,.0f}")

log("1/4 aggregating sales (slowest step)...")
cur.executescript(
    """
DROP TABLE IF EXISTS tmp_sales;
CREATE TABLE tmp_sales AS
SELECT VendorNo AS VendorNumber, Brand,
       SUM(SalesQuantity) AS TotalSalesQuantity,
       SUM(SalesDollars)  AS TotalSalesDollars,
       SUM(SalesPrice)    AS TotalSalesPrice,
       SUM(ExciseTax)     AS TotalExciseTax
FROM sales GROUP BY VendorNo, Brand;
CREATE INDEX idx_tmp_sales ON tmp_sales(VendorNumber, Brand);
"""
)

log("2/4 aggregating purchases, freight and brand list...")
cur.executescript(
    """
DROP TABLE IF EXISTS tmp_purch;
CREATE TABLE tmp_purch AS
SELECT VendorNumber, Brand,
       SUM(Quantity) AS TotalPurchaseQuantity,
       SUM(Dollars)  AS TotalPurchaseDollars
FROM purchases GROUP BY VendorNumber, Brand;
CREATE INDEX idx_tmp_purch ON tmp_purch(VendorNumber, Brand);

DROP TABLE IF EXISTS tmp_freight;
CREATE TABLE tmp_freight AS
SELECT VendorNumber, SUM(Freight) AS VendorFreight
FROM vendor_invoice GROUP BY VendorNumber;
CREATE INDEX idx_tmp_freight ON tmp_freight(VendorNumber);

DROP TABLE IF EXISTS tmp_base;
CREATE TABLE tmp_base AS
SELECT VendorNumber, Brand,
       MAX(VendorName)    AS VendorName,
       MAX(Description)   AS Description,
       MAX(Price)         AS ActualPrice,
       MAX(PurchasePrice) AS PurchasePrice
FROM purchase_prices GROUP BY VendorNumber, Brand;
CREATE INDEX idx_tmp_base ON tmp_base(VendorNumber, Brand);
"""
)

log("3/4 building final_summary_new...")
cur.executescript(
    """
DROP TABLE IF EXISTS final_summary_new;
CREATE TABLE final_summary_new AS
SELECT VendorNumber, VendorName, Brand, Description, ActualPrice, PurchasePrice,
       TotalSalesQuantity, TotalSalesDollars, TotalSalesPrice, TotalExciseTax,
       TotalPurchaseQuantity, TotalPurchaseDollars,
       CASE WHEN VendorPurch > 0
            THEN VendorFreight * TotalPurchaseDollars / VendorPurch ELSE 0 END AS TotalFreightCost,
       TotalSalesDollars - TotalPurchaseDollars AS GrossProfit,
       CASE WHEN TotalSalesDollars > 0
            THEN (TotalSalesDollars - TotalPurchaseDollars) * 100.0 / TotalSalesDollars ELSE 0 END AS Profitmargin,
       CASE WHEN TotalPurchaseQuantity > 0
            THEN TotalSalesQuantity * 1.0 / TotalPurchaseQuantity ELSE 0 END AS StockTurnover,
       CASE WHEN TotalPurchaseDollars > 0
            THEN TotalSalesDollars * 1.0 / TotalPurchaseDollars ELSE 0 END AS SalesPurchaseRatio
FROM (
    SELECT b.VendorNumber, b.VendorName, b.Brand, b.Description, b.ActualPrice, b.PurchasePrice,
           COALESCE(s.TotalSalesQuantity, 0)    AS TotalSalesQuantity,
           COALESCE(s.TotalSalesDollars, 0)     AS TotalSalesDollars,
           COALESCE(s.TotalSalesPrice, 0)       AS TotalSalesPrice,
           COALESCE(s.TotalExciseTax, 0)        AS TotalExciseTax,
           COALESCE(p.TotalPurchaseQuantity, 0) AS TotalPurchaseQuantity,
           COALESCE(p.TotalPurchaseDollars, 0)  AS TotalPurchaseDollars,
           COALESCE(f.VendorFreight, 0)         AS VendorFreight,
           SUM(COALESCE(p.TotalPurchaseDollars, 0)) OVER (PARTITION BY b.VendorNumber) AS VendorPurch
    FROM tmp_base b
    LEFT JOIN tmp_sales   s ON s.VendorNumber = b.VendorNumber AND s.Brand = b.Brand
    LEFT JOIN tmp_purch   p ON p.VendorNumber = b.VendorNumber AND p.Brand = b.Brand
    LEFT JOIN tmp_freight f ON f.VendorNumber = b.VendorNumber
);
"""
)

log("4/4 checking new totals against the raw tables...")
new = cur.execute(
    "SELECT COUNT(*), SUM(TotalSalesDollars), SUM(TotalPurchaseDollars), SUM(TotalFreightCost) FROM final_summary_new"
).fetchone()
raw_sales = cur.execute("SELECT SUM(TotalSalesDollars) FROM tmp_sales").fetchone()[0]
raw_purch = cur.execute("SELECT SUM(Dollars) FROM purchases").fetchone()[0]
raw_freight = cur.execute("SELECT SUM(Freight) FROM vendor_invoice").fetchone()[0]

print(f"\n{'':10}{'new':>20}{'raw':>20}")
print(f"{'rows':10}{new[0]:>20,}")
print(f"{'sales':10}{new[1]:>20,.0f}{raw_sales:>20,.0f}")
print(f"{'purchases':10}{new[2]:>20,.0f}{raw_purch:>20,.0f}")
print(f"{'freight':10}{new[3]:>20,.0f}{raw_freight:>20,.0f}\n")

ok = all(abs(n - r) / r <= TOLERANCE for n, r in [(new[1], raw_sales), (new[2], raw_purch), (new[3], raw_freight)])

if ok:
    cur.executescript("DROP TABLE final_summary; ALTER TABLE final_summary_new RENAME TO final_summary;")
    log("OK: totals match raw tables. final_summary replaced.")
else:
    log("NOT replaced: new totals are more than 2% away from raw. Old final_summary kept. Paste this output to Claude.")

cur.executescript(
    "DROP TABLE IF EXISTS tmp_sales; DROP TABLE IF EXISTS tmp_purch; "
    "DROP TABLE IF EXISTS tmp_freight; DROP TABLE IF EXISTS tmp_base;"
)
con.commit()
con.close()
sys.exit(0 if ok else 1)