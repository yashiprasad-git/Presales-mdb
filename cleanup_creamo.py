"""
One-time script: remove Cream-O (Context List only) from all DB tables.
Run once from the presales-mdb folder:
    python cleanup_creamo.py
"""

import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 not installed. Run: pip install psycopg2-binary")

# ── DB connection ──────────────────────────────────────────────────────────────
db_url = os.getenv("DATABASE_URL", "").strip()
if not db_url:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    secrets_path = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
    with open(secrets_path, "rb") as f:
        secrets = tomllib.load(f)
    db_url = secrets.get("DATABASE_URL", "")

if not db_url:
    sys.exit("DATABASE_URL not found in environment or .streamlit/secrets.toml")

conn = psycopg2.connect(db_url)

# ── Find Cream-O item IDs ──────────────────────────────────────────────────────
with conn.cursor() as cur:
    cur.execute("""
        SELECT monday_item_id, campaign_name, brand_name, region
        FROM campaigns
        WHERE LOWER(campaign_name) LIKE '%cream%o%'
           OR LOWER(brand_name)    LIKE '%cream%o%'
        ORDER BY monday_item_id
    """)
    rows = cur.fetchall()

if not rows:
    print("No Cream-O campaigns found in DB. Nothing to delete.")
    conn.close()
    sys.exit(0)

print("Found the following campaigns to delete:")
for r in rows:
    print(f"  item_id={r[0]}  name={r[1]}  brand={r[2]}  region={r[3]}")

confirm = input("\nDelete ALL of the above? (yes/no): ").strip().lower()
if confirm != "yes":
    print("Aborted — nothing deleted.")
    conn.close()
    sys.exit(0)

# ── Delete from all tables ─────────────────────────────────────────────────────
item_ids = [r[0] for r in rows]
tables = ["validation_results", "context_rows", "campaigns"]

with conn.cursor() as cur:
    for table in tables:
        cur.execute(
            f"DELETE FROM {table} WHERE monday_item_id = ANY(%s)",
            (item_ids,)
        )
        print(f"  Deleted {cur.rowcount} row(s) from {table}")

conn.commit()
conn.close()
print("\nDone — Cream-O removed from all tables.")
