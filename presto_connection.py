"""
Thin wrapper around the Presto Python client.

Executes a query and returns:
    (column_names, rows_as_dicts)
"""

from typing import Any, Dict, List, Optional, Tuple

import prestodb
from dotenv import load_dotenv

from config import settings

load_dotenv()


def _require_env(key: str):
    val = getattr(settings, key, None)

    if val is None:
        raise RuntimeError(
            f"Missing required environment variable: {key}"
        )

    return val


CATALOG = _require_env("PRESTO_CATALOG")
SCHEMA = _require_env("PRESTO_SCHEMA")
TABLE = _require_env("PRESTO_TABLE")

FQN = f'"{CATALOG}"."{SCHEMA}"."{TABLE}"'

DB_HOST = _require_env("PRESTO_HOST")
DB_PORT = int(settings.PRESTO_PORT)
DB_USER = _require_env("PRESTO_USER")
DB_PASS = _require_env("PRESTO_PASSWORD")


_presto_conn: Optional[prestodb.dbapi.Connection] = None


def get_connection() -> prestodb.dbapi.Connection:
    global _presto_conn

    if _presto_conn is None:
        _presto_conn = prestodb.dbapi.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            catalog=CATALOG,
            schema=SCHEMA,
            http_scheme="https",
            auth=prestodb.auth.BasicAuthentication(
                DB_USER,
                DB_PASS,
            ),
        )

    return _presto_conn


def run_query(
    sql: str,
    max_rows: int = None,
) -> Tuple[List[str], List[Dict[str, Any]]]:

    max_rows = max_rows or settings.MAX_ROWS

    conn = get_connection()

    try:
        cur = conn.cursor()
        cur.execute(sql)

        rows = cur.fetchmany(max_rows)

        columns = (
            [desc[0] for desc in cur.description]
            if cur.description
            else []
        )

        return columns, [
            dict(zip(columns, row))
            for row in rows
        ]

    finally:
        conn.close()


if __name__ == "__main__":
    query = """
    SELECT *
    FROM "purchase"."procurement_view"."purchase_unified_testing"
    """

    columns, rows = run_query(query)

    df = pd.DataFrame(rows, columns=columns)

    df.to_csv(
        "purchase_unified_testing.csv",
        index=False,
        encoding="utf-8-sig",   # UTF-8 with BOM for Excel
        quoting=csv.QUOTE_ALL,  # Quote all fields to preserve commas/newlines
        lineterminator="\n"     # Row separator
    )

    # Export to Excel
    # df.to_excel("purchase_vendor_budget.xlsx", index=False)

    print("Export completed successfully!")
    print(df.head())
