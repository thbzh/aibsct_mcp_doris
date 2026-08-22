"""Discovery tools: list_databases, list_tables, describe_table, table_overview."""

from __future__ import annotations

from typing import Any

from core.connection import ConnectionPool
from core.pagination import paginate
from core.response import ErrorCode, error_response, success_response


async def list_databases(
    pool: ConnectionPool,
    page_size: int = 50,
    page_token: str | None = None,
    db_whitelist: list[str] | None = None,
) -> str:
    """List all databases."""
    try:
        rows, _ = await pool.execute("SHOW DATABASES")
        databases = [r.get("Database") or r.get("database") or list(r.values())[0] for r in rows]

        # Filter internal databases
        databases = [d for d in databases if d and not d.startswith("__")]

        # Apply whitelist
        if db_whitelist:
            databases = [d for d in databases if d in db_whitelist]

        databases.sort()
        page, next_token, total = paginate(databases, page_size, page_token)
        meta: dict[str, Any] = {"total_count": total}
        if next_token:
            meta["next_page_token"] = next_token
        return success_response(page, meta)
    except Exception as e:
        return error_response(ErrorCode.CONNECTION_ERROR, str(e))


async def list_tables(
    pool: ConnectionPool,
    database: str,
    like: str | None = None,
    page_size: int = 50,
    page_token: str | None = None,
) -> str:
    """List table names in a database. Use describe_table for column detail."""
    try:
        sql = "SHOW TABLES"
        if like:
            sql += f" LIKE '{like}'"
        rows, _ = await pool.execute(sql, database=database)
        table_names = [list(r.values())[0] for r in rows]
        table_names.sort()

        page, next_token, total = paginate(table_names, page_size, page_token)
        meta: dict[str, Any] = {"total_count": total, "database": database}
        if next_token:
            meta["next_page_token"] = next_token
        return success_response(page, meta)
    except Exception as e:
        return error_response(ErrorCode.CONNECTION_ERROR, str(e))


async def describe_table(
    pool: ConnectionPool,
    database: str,
    table: str,
    detail_level: str = "summary",
) -> str:
    """Describe a table's structure."""
    try:
        # Basic columns
        rows, _ = await pool.execute(f"DESCRIBE `{table}`", database=database)
        columns = []
        for r in rows:
            col: dict[str, Any] = {
                "name": r.get("Field", ""),
                "type": r.get("Type", ""),
            }
            if detail_level in ("summary", "full"):
                col["null"] = r.get("Null", "")
                col["key"] = r.get("Key", "")
                col["default"] = r.get("Default")
                col["extra"] = r.get("Extra", "")
            columns.append(col)

        result: dict[str, Any] = {
            "database": database,
            "table": table,
            "columns": columns,
        }

        if detail_level == "full":
            # Get CREATE TABLE for partitions, distribution, properties
            try:
                ct_rows, _ = await pool.execute(
                    f"SHOW CREATE TABLE `{table}`", database=database
                )
                if ct_rows:
                    create_sql = list(ct_rows[0].values())[-1] if ct_rows[0] else ""
                    result["create_table"] = create_sql
            except Exception:
                result["create_table"] = None

            # Get partition info
            try:
                part_rows, _ = await pool.execute(
                    f"SHOW PARTITIONS FROM `{table}`", database=database
                )
                result["partitions"] = part_rows
            except Exception:
                result["partitions"] = []

        meta = {"database": database, "table": table}
        return success_response(result, meta)
    except Exception as e:
        return error_response(ErrorCode.CONNECTION_ERROR, str(e))


_TABLE_OVERVIEW_SQL = """
WITH partition_ranked AS (
    SELECT
        TABLE_SCHEMA,
        TABLE_NAME,
        PARTITION_NAME,
        TABLE_ROWS AS latest_part_rows,
        DATA_LENGTH AS latest_part_bytes,
        ROW_NUMBER() OVER (
            PARTITION BY TABLE_SCHEMA, TABLE_NAME
            ORDER BY UPDATE_TIME DESC, PARTITION_ID DESC
        ) AS rn
    FROM information_schema.partitions
),
partition_agg AS (
    SELECT
        TABLE_SCHEMA,
        TABLE_NAME,
        COUNT(1) AS partition_count,
        MAX(UPDATE_TIME) AS latest_update_time,
        SUM(TABLE_ROWS) AS total_rows,
        SUM(DATA_LENGTH) AS total_bytes
    FROM information_schema.partitions
    GROUP BY TABLE_SCHEMA, TABLE_NAME
)
SELECT
    t.TABLE_SCHEMA AS `数据库`,
    t.TABLE_NAME AS `表名`,
    t.TABLE_TYPE AS `表类型`,
    p.PARTITION_NAME AS `最新更新分区`,
    agg.partition_count AS `分区数量`,
    t.CREATE_TIME AS `创建时间`,
    agg.latest_update_time AS `最近更新时间`,
    agg.total_rows AS `总数据行数`,
    ROUND(agg.total_bytes / 1024 / 1024, 2) AS `总数据量(MB)`,
    p.latest_part_rows AS `最近更新分区行数`,
    ROUND(p.latest_part_bytes / 1024 / 1024, 2) AS `最近更新分区数据量(MB)`
FROM information_schema.tables t
JOIN partition_agg agg
  ON t.TABLE_SCHEMA = agg.TABLE_SCHEMA AND t.TABLE_NAME = agg.TABLE_NAME
JOIN (
    SELECT TABLE_SCHEMA, TABLE_NAME, PARTITION_NAME, latest_part_rows, latest_part_bytes
    FROM partition_ranked
    WHERE rn = 1
) p
  ON t.TABLE_SCHEMA = p.TABLE_SCHEMA AND t.TABLE_NAME = p.TABLE_NAME
WHERE t.TABLE_SCHEMA NOT IN ('information_schema', '__internal_schema')
"""


async def table_overview(
    pool: ConnectionPool,
    database: str | None = None,
    tables: list[str] | None = None,
    db_whitelist: list[str] | None = None,
) -> str:
    """Overview of tables: latest partition, partition count, create/update time, data volume.

    When both *database* and *tables* are ``None``, returns ALL tables
    across all (non-internal) databases, optionally restricted to
    *db_whitelist*.
    """
    if tables:
        # 全限定名（"db.table"）自携带库信息，可独立使用；
        # 裸表名依赖 database 参数消歧，缺失时直接报错指引，
        # 而不是静默返回全部表（那会让调用方误以为查询成功）。
        bare = [t for t in tables if "." not in t]
        if bare and not database:
            return error_response(
                ErrorCode.VALIDATION_ERROR,
                f"Bare table name(s) {bare} are ambiguous without a database. "
                "Fix: pass database='your_db' together, or use fully "
                "qualified name(s) like 'your_db.your_table'.",
            )
        # 参数里可能混入空白字符（LLM 常见）——规范化
        tables = [t.strip() for t in tables if t and t.strip()]

    try:
        conditions: list[str] = []
        params: list[str] = []

        if database:
            conditions.append("t.TABLE_SCHEMA = %s")
            params.append(database)
        elif db_whitelist:
            conditions.append(
                "t.TABLE_SCHEMA IN ({})".format(", ".join("%s" for _ in db_whitelist))
            )
            params.extend(db_whitelist)
        if tables:
            qualified = [t for t in tables if "." in t]
            if qualified:
                # Doris Nereids 不支持行值构造器 (a, b) IN ((x, y))，
                # 也不推荐 OR 链展开（多表时条件膨胀）。
                # 用两个 IN 的交集：SCHEMA IN (...) AND NAME IN (...)。
                # 注意：这是宽松匹配（笛卡尔交集），可能多出
                # "库名 x 表名" 的交叉组合行；单库或单表场景下精确。
                schemas = list(dict.fromkeys(s.split(".", 1)[0] for s in qualified))
                names = list(dict.fromkeys(s.split(".", 1)[1] for s in qualified))
                conditions.append(
                    "t.TABLE_SCHEMA IN ({}) AND t.TABLE_NAME IN ({})".format(
                        ", ".join("%s" for _ in schemas),
                        ", ".join("%s" for _ in names),
                    )
                )
                params.extend(schemas)
                params.extend(names)
            if database:
                bare = [t for t in tables if "." not in t]
                if bare:
                    conditions.append(
                        "t.TABLE_NAME IN ({})".format(", ".join("%s" for _ in bare))
                    )
                    params.extend(bare)

        sql = _TABLE_OVERVIEW_SQL
        if conditions:
            sql += "  AND " + "\n  AND ".join(conditions)
        # 排序列必须带表前缀：t/agg/p 三个 JOIN 源都暴露 TABLE_SCHEMA、
        # TABLE_NAME，裸列名会让 Doris Nereids 报 "TABLE_SCHEMA is ambiguous"。
        sql += "\nORDER BY t.TABLE_SCHEMA, t.TABLE_NAME"

        rows, columns = await pool.execute(sql, params=params, max_rows=1000)
        meta: dict[str, Any] = {"row_count": len(rows)}
        if database:
            meta["database"] = database
        if tables:
            meta["tables"] = tables
        return success_response({"columns": columns, "rows": rows}, meta)
    except Exception as e:
        return error_response(ErrorCode.CONNECTION_ERROR, str(e))
