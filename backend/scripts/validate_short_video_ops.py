from __future__ import annotations

import json
from pathlib import Path

import duckdb

from short_video_ops_definitions import DATABASE, PRIMARY_KEYS, RELATIONS, ROLE_TABLES, TABLE_META


DATABASE_DIR = Path(__file__).resolve().parents[1] / "data" / "databases" / DATABASE


def validate() -> dict:
    manifest = json.loads((DATABASE_DIR / "_database_manifest.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    connection = duckdb.connect(":memory:")
    try:
        for table in TABLE_META:
            csv_path = (DATABASE_DIR / f"{table}.csv").resolve().as_posix().replace("'", "''")
            connection.execute(
                f'CREATE VIEW "{table}" AS SELECT * FROM read_csv_auto(\'{csv_path}\', header=true, sample_size=-1)'
            )
            keys = PRIMARY_KEYS[table]
            key_sql = ", ".join(f'"{key}"' for key in keys)
            duplicate_count = connection.execute(
                f'SELECT COUNT(*) FROM (SELECT {key_sql}, COUNT(*) AS n FROM "{table}" GROUP BY {key_sql} HAVING n > 1)'
            ).fetchone()[0]
            if duplicate_count:
                errors.append(f"{table}存在{duplicate_count}组重复主键")

        for left_table, left_field, right_table, right_field, description in RELATIONS:
            missing = connection.execute(
                f'''SELECT COUNT(*)
                    FROM "{right_table}" AS child
                    LEFT JOIN "{left_table}" AS parent
                      ON child."{right_field}" = parent."{left_field}"
                    WHERE child."{right_field}" IS NOT NULL
                      AND CAST(child."{right_field}" AS VARCHAR) <> ''
                      AND parent."{left_field}" IS NULL'''
            ).fetchone()[0]
            if missing:
                errors.append(f"{description}存在{missing}条未匹配记录")
    finally:
        connection.close()

    for role, tables in ROLE_TABLES.items():
        unknown = set(tables) - set(TABLE_META)
        if unknown:
            errors.append(f"{role}包含未知表：{', '.join(sorted(unknown))}")
        if len(tables) > 20:
            errors.append(f"{role}可见表超过20张")

    return {
        "database": DATABASE,
        "tables": manifest["table_count"],
        "rows": manifest["total_rows"],
        "role_table_counts": {role: len(tables) for role, tables in ROLE_TABLES.items()},
        "errors": errors,
    }


if __name__ == "__main__":
    result = validate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result["errors"] else 0)
