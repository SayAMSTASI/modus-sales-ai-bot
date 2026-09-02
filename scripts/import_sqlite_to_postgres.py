from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

import psycopg
from psycopg import sql

TABLES = (
    "user_access",
    "update_jobs",
    "conversation_messages",
    "usage_events",
    "user_question_audit",
    "pending_conversation_feedback",
    "conversation_feedback",
    "admin_audit",
    "oauth_credentials",
    "oauth_device_sessions",
    "oauth_authorization_sessions",
    "skill_versions",
    "skill_edit_sessions",
)


def postgres_dsn(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def read_source(source: Path) -> dict[str, tuple[list[str], list[tuple]]]:
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        result: dict[str, tuple[list[str], list[tuple]]] = {}
        for table in TABLES:
            if table not in existing:
                continue
            columns = [
                row[1]
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'  # noqa: S608 - whitelist only
                )
            ]
            rows = list(
                connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY id'  # noqa: S608 - whitelist only
                )
            )
            result[table] = (columns, rows)
        return result
    finally:
        connection.close()


def import_rows(
    data: dict[str, tuple[list[str], list[tuple]]], database_url: str
) -> None:
    with psycopg.connect(postgres_dsn(database_url)) as connection:
        with connection.cursor() as cursor:
            nonempty: list[str] = []
            for table in data:
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                )
                if cursor.fetchone()[0]:
                    nonempty.append(table)
            if nonempty:
                names = ", ".join(nonempty)
                raise RuntimeError(
                    "Target PostgreSQL is not empty. Refusing to merge tables: " + names
                )

            for table, (columns, rows) in data.items():
                if not rows:
                    continue
                query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                )
                cursor.executemany(query, rows)
                cursor.execute(
                    sql.SQL(
                        "SELECT setval(pg_get_serial_sequence({}, 'id'), "
                        "COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {}"
                    ).format(sql.Literal(table), sql.Identifier(table))
                )
        connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a complete Sales Bot SQLite backup into fresh PostgreSQL."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Target PostgreSQL URL; defaults to DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and count the source without connecting to PostgreSQL.",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        raise SystemExit(f"SQLite backup not found: {args.source}")
    data = read_source(args.source)
    summary = ", ".join(f"{table}={len(rows)}" for table, (_, rows) in data.items())
    print("source-ok " + summary)
    if args.dry_run:
        return
    if not args.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise SystemExit("A PostgreSQL DATABASE_URL is required.")
    import_rows(data, args.database_url)
    print("import-ok " + summary)


if __name__ == "__main__":
    main()
