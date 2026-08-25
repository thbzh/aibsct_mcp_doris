"""Data quality tools: run_data_quality_check.

按需数据质量校验工具，参考批量脚本 src/tools/dataquality.py 的规则装配逻辑。

与批量脚本的区别：
- 入参为 database + 表 list（无需调度参数占位符 ${dt} 等）
- 元数据实时从 information_schema 读取（不依赖 stg_db 快照表）
- 校验结果直接以 JSON 返回（不落 stg_dq_validation_results）
- 复用 per-user 连接池的凭据建立 GX 数据源
"""

from __future__ import annotations

import asyncio
import datetime
import urllib.parse
from typing import Any

from core.connection import ConnectionPool
from core.response import ErrorCode, error_response, success_response

# ==================== 元数据获取 ====================

_META_COLUMNS_SQL = """
SELECT
    TABLE_NAME  AS table_name,
    COLUMN_NAME AS column_name,
    DATA_TYPE   AS data_type,
    IS_NULLABLE AS is_nullable,
    COLUMN_KEY  AS column_key,
    ORDINAL_POSITION AS ordinal_position
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s
  AND TABLE_NAME = %s
ORDER BY ORDINAL_POSITION ASC
"""

_META_PARTITION_SQL = """
SELECT
    TABLE_SCHEMA   AS table_schema,
    TABLE_NAME     AS table_name,
    PARTITION_NAME AS partition_name,
    UPDATE_TIME    AS update_time,
    TABLE_ROWS     AS table_rows
FROM information_schema.PARTITIONS
WHERE TABLE_SCHEMA = %s
  AND TABLE_NAME = %s
  AND TABLE_ROWS > 0
ORDER BY UPDATE_TIME DESC
LIMIT 1
"""


async def _fetch_table_meta(
    pool: ConnectionPool, database: str, table: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """读取一张表的列元数据与最新有数据的分区。"""
    rows, _ = await pool.execute(_META_COLUMNS_SQL, params=[database, table], max_rows=5000)
    columns = list(rows)
    if not columns:
        return [], None

    try:
        part_rows, _ = await pool.execute(
            _META_PARTITION_SQL, params=[database, table], max_rows=1
        )
        partition = part_rows[0] if part_rows else None
    except Exception:
        partition = None
    return columns, partition


# ==================== 规则装配 + 执行（同步，在线程中运行） ====================

def _build_suite_for_table(
    suite: Any,
    gxe: Any,
    table_name: str,
    partition_name: str | None,
    columns: list[dict[str, Any]],
) -> None:
    """按批量脚本的六维规则自动装配 ExpectationSuite。"""
    # ---- 表级：完整性（数据量不为 0）----
    suite.add_expectation(
        gxe.ExpectTableRowCountToBeBetween(
            min_value=1,
            meta={
                "notes": "【完整性】表/分区数据量不能为 0",
                "dimension": "completeness",
                "weight": 3,
            },
        )
    )

    # ---- 表级：可用性（表结构漂移/多列/少列/顺序错乱）----
    column_list = [c["column_name"] for c in columns]
    suite.add_expectation(
        gxe.ExpectTableColumnsToMatchOrderedList(
            column_list=column_list,
            meta={
                "notes": "【可用性】目标表结构是否发生漂移、多列、少列或顺序错乱",
                "dimension": "availability",
                "weight": 3,
            },
        )
    )

    uni_columns = [c["column_name"] for c in columns if c.get("column_key") == "UNI"]

    for col in columns:
        col_name = col["column_name"]
        data_type = (col.get("data_type") or "").lower()
        is_nullable = (col.get("is_nullable") or "").upper()
        column_key = col.get("column_key")

        is_numeric = any(t in data_type for t in ["int", "decimal", "double", "float", "number"])
        is_string = any(t in data_type for t in ["char", "varchar", "text", "string"])

        # ---- 完整性：空值率 ----
        if column_key == "PRI" or col_name.endswith(("_id", "_code")) or is_nullable == "NO":
            suite.add_expectation(
                gxe.ExpectColumnValuesToNotBeNull(
                    column=col_name,
                    mostly=1.0,
                    meta={
                        "notes": "【非空】标识/外键列严禁存在任何空值",
                        "dimension": "completeness",
                        "weight": 3,
                    },
                )
            )
        else:
            suite.add_expectation(
                gxe.ExpectColumnValuesToNotBeNull(
                    column=col_name,
                    mostly=0.90,
                    meta={
                        "notes": "【空值率】业务字段空值率不应超过 10%",
                        "dimension": "completeness",
                        "weight": 3,
                    },
                )
            )

        # ---- 一致性：唯一性（表级，装配一次）----
        if uni_columns:
            if len(uni_columns) == 1:
                suite.add_expectation(
                    gxe.ExpectColumnValuesToBeUnique(
                        column=uni_columns[0],
                        meta={
                            "notes": "【唯一性】单列唯一键数据严禁存在重复值",
                            "dimension": "consistency",
                            "weight": 3,
                        },
                    )
                )
            else:
                suite.add_expectation(
                    gxe.ExpectCompoundColumnsToBeUnique(
                        column_list=uni_columns,
                        meta={
                            "notes": "【联合唯一性】联合主键组合值严禁存在重复值",
                            "dimension": "consistency",
                            "weight": 3,
                        },
                    )
                )

        # ---- 准确性 ----
        # 3.1 数值边界
        if is_numeric:
            suite.add_expectation(
                gxe.ExpectColumnValuesToBeBetween(
                    column=col_name,
                    min_value=-99999999,
                    max_value=99999999,
                    meta={
                        "notes": "【边界值】数值列的所有非空值均应在合理边界内（允许为空）",
                        "dimension": "accuracy",
                        "weight": 2,
                    },
                )
            )
        # 3.2 字符枚举基数
        if is_string and col_name.endswith("_type"):
            suite.add_expectation(
                gxe.ExpectColumnUniqueValueCountToBeBetween(
                    column=col_name,
                    min_value=0,
                    max_value=200,
                    meta={
                        "notes": "【基数监控】字符列的去重枚举数应控制在 [0, 200] 之间",
                        "dimension": "accuracy",
                        "weight": 2,
                    },
                )
            )
        # 3.3 数值合理性（非负/区间）
        if is_numeric:
            if any(k in col_name for k in ("percent", "rate", "ratio")):
                suite.add_expectation(
                    gxe.ExpectColumnValuesToBeBetween(
                        column=col_name,
                        min_value=0.0,
                        max_value=100.0,
                        meta={
                            "notes": "【数值合理性】比例/率字段的值必须在 [0, 100] 之间",
                            "dimension": "accuracy",
                            "weight": 2,
                        },
                    )
                )
            elif any(k in col_name for k in ("price", "amount", "money", "salary", "qty", "count")):
                suite.add_expectation(
                    gxe.ExpectColumnValuesToBeBetween(
                        column=col_name,
                        min_value=0.0,
                        meta={
                            "notes": "【数值合理性】金额/数量字段严禁存在负数",
                            "dimension": "accuracy",
                            "weight": 2,
                        },
                    )
                )
        # 3.4 时间列不超当前日期
        if col_name in ("update_time", "create_time", "gmt_modified") and "date" in data_type:
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            suite.add_expectation(
                gxe.ExpectColumnValuesToBeBetween(
                    column=col_name,
                    max_value=today_str,
                    meta={
                        "notes": "【准确性】时间列值不应超过当前日期，防止导入逻辑异常",
                        "dimension": "accuracy",
                        "weight": 2,
                    },
                )
            )
        # 3.5 删除标识值域
        if col_name in ("is_deleted", "is_delete", "deleted"):
            allowed_set = [0, 1] if is_numeric else ["0", "1"]
            suite.add_expectation(
                gxe.ExpectColumnValuesToBeInSet(
                    column=col_name,
                    value_set=allowed_set,
                    meta={
                        "notes": f"【值域规范】删除标识只能是 {allowed_set}",
                        "dimension": "accuracy",
                        "weight": 2,
                    },
                )
            )


def _extract_check_rows(validation_result: Any) -> list[dict[str, Any]]:
    """把 GX 校验结果统一抽取为 dict 行（兼容 dict/对象两种返回形态）。"""
    is_dict = isinstance(validation_result, dict)
    checks = validation_result.get("results", []) if is_dict else getattr(validation_result, "results", [])

    parsed: list[dict[str, Any]] = []
    for check in checks:
        d = check if isinstance(check, dict) else None
        g = lambda k, default=None: (check.get(k, default) if d else getattr(check, k, default))

        success = bool(g("success", False))
        config = g("expectation_config")
        if config is None:
            continue
        cd = config if isinstance(config, dict) else None
        cg = lambda k, default=None: (config.get(k, default) if cd else getattr(config, k, default))

        expectation_type = cg("expectation_type") or cg("type") or ""
        kwargs = cg("kwargs") or {}
        kd = kwargs if isinstance(kwargs, dict) else None
        kg = lambda k, default=None: (kwargs.get(k, default) if kd else getattr(kwargs, k, default))

        meta = cg("meta") or {}
        md = meta if isinstance(meta, dict) else None
        mg = lambda k, default=None: (meta.get(k, default) if md else getattr(meta, k, default))

        res = g("result") or {}
        rd = res if isinstance(res, dict) else None
        rg = lambda k, default=None: (res.get(k, default) if rd else getattr(res, k, default))

        exp_min, exp_max = kg("min_value"), kg("max_value")
        if exp_min is not None or exp_max is not None:
            expected_value = f"[{exp_min}, {exp_max}]"
        else:
            expected_value = str(kg("value") or "") or None

        parsed.append({
            "column": kg("column"),
            "expectation_type": expectation_type,
            "success": success,
            "element_count": rg("element_count"),
            "unexpected_count": rg("unexpected_count"),
            "unexpected_percent": rg("unexpected_percent"),
            "observed_value": rg("observed_value"),
            "expected_value": expected_value,
            "rule_notes": mg("notes", ""),
            "dimension": mg("dimension", ""),
            "weight": mg("weight"),
        })
    return parsed


def _run_validation_sync(
    connection_string: str,
    database: str,
    table: str,
    columns: list[dict[str, Any]],
    partition: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """同步执行单表校验（在线程池中调用），返回校验明细行。"""
    import great_expectations as gx
    import great_expectations.expectations as gxe

    context = gx.get_context(mode="ephemeral")

    datasource = context.data_sources.add_sql(
        name=f"dq_ds_{database}_{table}",
        connection_string=connection_string,
    )

    partition_name = (partition or {}).get("partition_name")
    is_partitioned = partition_name is not None and partition_name != table

    if is_partitioned:
        asset_name = f"asset_{table}_{partition_name}"
        query_sql = f"SELECT * FROM `{database}`.`{table}` PARTITION ({partition_name})"
        asset = datasource.add_query_asset(name=asset_name, query=query_sql)
    else:
        asset_name = f"asset_{table}"
        asset = datasource.add_table_asset(name=asset_name, table_name=table)

    batch_definition = asset.add_batch_definition_whole_table(name=f"batch_{table}")

    suite_name = f"suite_{table}"
    suite = context.suites.add(gx.core.expectation_suite.ExpectationSuite(name=suite_name))
    _build_suite_for_table(suite, gxe, table, partition_name, columns)

    val_def = gx.ValidationDefinition(
        name=f"val_def_{table}",
        data=batch_definition,
        suite=suite,
    )
    val_def = context.validation_definitions.add_or_update(val_def)
    validation_result = val_def.run()
    return _extract_check_rows(validation_result)


# ==================== 工具入口 ====================

async def run_data_quality_check(
    pool: ConnectionPool,
    database: str,
    tables: list[str] | str,
) -> str:
    """按需校验指定库下表 list 的数据质量，返回逐规则明细。"""
    if isinstance(tables, str):
        tables = [tables] if tables else []
    tables = [t.strip() for t in tables if t and t.strip()]
    # 允许传入 "db.table" 全限定名（忽略 database 前缀）
    normalized: list[str] = []
    for t in tables:
        if "." in t:
            t = t.split(".", 1)[1]
        normalized.append(t)
    if not normalized:
        return error_response(ErrorCode.INVALID_PARAMS, "tables 不能为空")
    if len(normalized) > 20:
        return error_response(
            ErrorCode.INVALID_PARAMS, "单次最多校验 20 张表，请分批调用"
        )

    # great_expectations 核心包不带 sqlalchemy（只在其 extras 里声明）。
    # 启动环境缺依赖时给出明确指引，而不是每张表都抛 ModuleNotFoundError。
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        return error_response(
            ErrorCode.SERVICE_NOT_READY,
            "服务端缺少 sqlalchemy，无法执行数据质量校验。"
            "请安装依赖: pip install 'great_expectations[mysql]'",
        )

    try:
        connection_string = pool.sqlalchemy_url(database)
    except Exception as e:
        return error_response(ErrorCode.CONNECTION_ERROR, str(e))

    table_reports: list[dict[str, Any]] = []
    failed_tables: list[dict[str, Any]] = []
    total_rules = 0
    failed_rules = 0

    for table in normalized:
        try:
            columns, partition = await _fetch_table_meta(pool, database, table)
            if not columns:
                failed_tables.append({"table": table, "error": "表不存在或无列元数据"})
                continue

            check_rows = await asyncio.to_thread(
                _run_validation_sync, connection_string, database, table, columns, partition
            )

            table_failed = [c for c in check_rows if not c["success"]]
            total_rules += len(check_rows)
            failed_rules += len(table_failed)

            table_reports.append({
                "table": table,
                "partition": (partition or {}).get("partition_name"),
                "update_time": (partition or {}).get("update_time"),
                "rule_count": len(check_rows),
                "failed_rule_count": len(table_failed),
                "passed": len(table_failed) == 0,
                "checks": check_rows,
            })
        except Exception as e:
            failed_tables.append({"table": table, "error": str(e)})

    data: dict[str, Any] = {
        "database": database,
        "tables_checked": len(table_reports),
        "total_rules": total_rules,
        "failed_rules": failed_rules,
        "all_passed": not failed_rules and not failed_tables,
        "table_reports": table_reports,
    }
    if failed_tables:
        data["failed_tables"] = failed_tables
        data["all_passed"] = False

    meta = {
        "run_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tables_requested": len(normalized),
    }
    return success_response(data, meta)
