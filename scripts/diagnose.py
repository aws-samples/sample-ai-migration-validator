"""Diagnostic script: verify a connection and dump what the validator sees.

Usage (from project root):

    .venv/bin/python scripts/diagnose.py source
    .venv/bin/python scripts/diagnose.py target

The script prompts for the same connection details the validator does, then:
  1. opens a direct connection (no MCP),
  2. prints server/db/user info,
  3. lists every visible schema,
  4. for the chosen schema, prints object counts and table row counts,
  5. exits.

If this works but the validator still shows 0/0, it's an MCP transport bug —
not a database one. Either way the output tells you exactly what the database
returned.
"""

from __future__ import annotations

import getpass
import sys


def diagnose_sqlserver() -> None:
    import pytds

    print("--- SQL Server diagnostic ---")
    host = input("Host: ").strip()
    port = int(input("Port [1433]: ").strip() or "1433")
    db = input("Database: ").strip()
    user = input("Username: ").strip()
    pwd = getpass.getpass("Password: ")
    schema = input("Schema [dbo]: ").strip() or "dbo"

    conn = pytds.connect(
        server=host,
        port=port,
        database=db,
        user=user,
        password=pwd,
        as_dict=True,
        autocommit=True,
        readonly=True,
        login_timeout=10,
    )
    cur = conn.cursor()

    cur.execute("SELECT @@SERVERNAME AS server, DB_NAME() AS db, USER_NAME() AS db_user")
    print("\nIdentity:", cur.fetchone())

    cur.execute("SELECT DISTINCT table_schema FROM INFORMATION_SCHEMA.TABLES ORDER BY 1")
    print("Visible schemas:", [r["table_schema"] for r in cur.fetchall()])

    print(f"\nObject counts in schema '{schema}':")
    cur.execute(
        """
        SELECT 'tables' AS bucket, COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLES
          WHERE table_schema = %s AND table_type = 'BASE TABLE'
        UNION ALL
        SELECT 'views', COUNT(*) FROM INFORMATION_SCHEMA.VIEWS WHERE table_schema = %s
        UNION ALL
        SELECT 'routines', COUNT(*) FROM INFORMATION_SCHEMA.ROUTINES WHERE routine_schema = %s
        """,
        (schema, schema, schema),
    )
    for r in cur.fetchall():
        print(f"  {r['bucket']:>10}: {r['n']}")

    print(f"\nFirst 20 tables in '{schema}':")
    cur.execute(
        """SELECT TOP 20 table_name FROM INFORMATION_SCHEMA.TABLES
            WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name""",
        (schema,),
    )
    for r in cur.fetchall():
        print(f"  {r['table_name']}")

    conn.close()


def diagnose_postgres() -> None:
    import psycopg
    from psycopg.rows import dict_row

    print("--- PostgreSQL diagnostic ---")
    host = input("Host: ").strip()
    port = int(input("Port [5432]: ").strip() or "5432")
    db = input("Database: ").strip()
    user = input("Username: ").strip()
    pwd = getpass.getpass("Password: ")
    schema = input("Schema [public]: ").strip() or "public"

    conn = psycopg.connect(
        host=host,
        port=port,
        dbname=db,
        user=user,
        password=pwd,
        sslmode="require",
        connect_timeout=10,
        row_factory=dict_row,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT current_database() AS db, current_user AS u, version() AS v")
        info = cur.fetchone()
        print("\nIdentity:", info)

        cur.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog', 'information_schema') ORDER BY schema_name"
        )
        print("Visible schemas:", [r["schema_name"] for r in cur.fetchall()])

        print(f"\nObject counts in schema '{schema}':")
        cur.execute(
            """
            SELECT 'tables' AS bucket,
                   (SELECT count(*) FROM information_schema.tables
                     WHERE table_schema = %s AND table_type = 'BASE TABLE') AS n
            UNION ALL
            SELECT 'views',    (SELECT count(*) FROM information_schema.views WHERE table_schema = %s)
            UNION ALL
            SELECT 'routines', (SELECT count(*) FROM information_schema.routines WHERE routine_schema = %s)
            """,
            (schema, schema, schema),
        )
        for r in cur.fetchall():
            print(f"  {r['bucket']:>10}: {r['n']}")

        print(f"\nFirst 20 tables in '{schema}':")
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "ORDER BY table_name LIMIT 20",
            (schema,),
        )
        for r in cur.fetchall():
            print(f"  {r['table_name']}")
    conn.close()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("source", "target"):
        print("usage: python scripts/diagnose.py {source|target}")
        return 1
    if sys.argv[1] == "source":
        diagnose_sqlserver()
    else:
        diagnose_postgres()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
