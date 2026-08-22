# Doris MCP 查询技能

> 你因为调用了 `get_query_guide()` 而读到这段内容。以下所有工具调用规则均为强制要求--请严格遵守。

## 摘要 - 七个核心工具

```
get_query_guide()                        -> 你在这里（已调用）
check_service_health()                   -> 哪个工作区是健康的？
list_metrics(workspace)                  -> 我可以问什么？
list_dimensions_for_metric(workspace, name) -> 如何切片？
query_metric(workspace, metrics, ...)    -> 给我数据
table_overview(database, tables)         -> 表概览：分区/数据量/更新时间
execute_query(sql, ...)                  -> 原始 SQL，仅作最后手段
```

`workspace` 是前三个工具的必填参数。内置示例使用 `"example"`。

---

## 第 0 步：检查健康状态（始终作为第二步 - get_query_guide 已调用）

收到本指南后立即调用：

```
check_service_health()
```

返回示例：

```json
{
  "doris": "connected",
  "workspaces": {
    "example":   {"status": "healthy",    "metric_count": 5},
    "marketing": {"status": "no_models",  "message": "No YAML files"},
    "finance":   {"status": "not_ready",  "message": "Files present but failed to load"}
  }
}
```

**规则：**
- 选择 `status: "healthy"` 的工作区--只有 `query_metric` 能在其中运行。
- 如果用户提到特定工作区，使用该工作区。否则使用 `"example"`。
- 如果 `doris` 显示为 `"unavailable"`，警告用户。`list_databases` / `execute_query` 仍可能可用。
- 如果没有任何工作区处于健康状态 -> 回退到原始 SQL 路径（见文末）。

---

## 第 1 步：list_metrics - 我可以问什么？

```json
// 请求
{"workspace": "example"}

// 响应
{
  "data": [
    {"name": "total_amount", "description": "订单总金额"},
    {"name": "order_count",   "description": "订单数量"},
    {"name": "avg_amount",    "description": "平均订单金额"},
    {"name": "unique_users",  "description": "下单用户数"},
    {"name": "user_count",    "description": "用户数量"}
  ],
  "meta": {"total_count": 5}
}
```

**如何将用户意图匹配到指标：**
- "销售额 / 收入 / GMV" -> `total_amount`
- "订单 / 交易量" -> `order_count`
- "平均订单金额 / AOV" -> `avg_amount`
- "下单用户 / 购买用户" -> `unique_users`
- "用户 / 客户数" -> `user_count`

如果用户的问题与任何指标都不明确匹配，调用 `list_metrics` 并浏览所有描述。如果仍无匹配，回退到原始 SQL。

---

## 第 2 步：list_dimensions_for_metric - 如何切片？

```json
// 请求
{"workspace": "example", "metric_name": "total_amount"}

// 响应
{
  "data": [
    {"name": "order_date",    "type": "time",        "description": "订单日期（日粒度）"},
    {"name": "channel",       "type": "categorical", "description": "订单渠道"},
    {"name": "status",        "type": "categorical", "description": "订单状态"},
    {"name": "city",          "type": "categorical", "description": "城市"},
    {"name": "level",         "type": "categorical", "description": "客户等级"},
    {"name": "register_date", "type": "time",        "description": "注册日期"},
    {"name": "category",      "type": "categorical", "description": "商品类别"},
    {"name": "brand",         "type": "categorical", "description": "品牌"}
  ],
  "meta": {"metric": "total_amount", "count": 8}
}
```

**规则：**
- `type: "time"` -> 可按 天/周/月/季/年 分组。在 `group_by` 中使用 `"month"`。
- `type: "categorical"` -> 离散分桶。使用 `"channel"`、`"city"` 等。
- 引擎自动跨表关联。`city` 来自 `users` 表，但可与 `orders` 表的 `total_amount` 配合使用--无需手动 JOIN。
- 在调用 `query_metric` 之前始终检查维度。使用不在列表中的维度会导致错误。

---

## 第 3 步：query_metric - 获取数据

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|-------|------|----------|---------|-------------|
| `workspace` | string | **是** | - | `"example"` |
| `metrics` | list[string] | **是** | - | 例如 `["total_amount", "order_count"]` |
| `group_by` | list[string] | 否 | `[]` | 来自第 2 步的维度名。时间粒度：`"day"`、`"week"`、`"month"`、`"quarter"`、`"year"` |
| `where` | string | 否 | `""` | SQL 谓词或 JSON 对象 |
| `order_by` | list[string] | 否 | `[]` | `-` 前缀 = 降序，例如 `["-total_amount"]` |
| `limit` | int | 否 | `0` | 最大行数。`0` = 不限制 |
| `having` | string | 否 | `""` | 对聚合值过滤，例如 `"total_amount > 1000"` |
| `database` | string | 否 | `""` | 目标 Doris 数据库（为空时自动检测） |
| `max_rows` | int | 否 | `0` | 执行时的硬性行数上限。`0` = 服务端默认值（10,000） |

### 响应

```json
{
  "data": {
    "columns": ["channel", "total_amount"],
    "rows": [
      {"channel": "APP",  "total_amount": 2396.00},
      {"channel": "WEB",  "total_amount": 2096.00},
      {"channel": "MINI", "total_amount": 298.00}
    ]
  },
  "meta": {"duration_ms": 12.5, "row_count": 3}
}
```

### WHERE 语法

以下形式均会自动归一化--选择最简单的一种即可：

```python
# 纯 SQL
where="channel = 'APP'"
where="order_date >= '2026-02-01' AND order_date <= '2026-02-28'"
where="channel IN ('APP', 'MINI')"

# JSON 对象（AND 连接）
where='{"channel": "APP", "status": "completed"}'

# 带数组值的 JSON（IN 子句）
where='{"channel": ["APP", "MINI"]}'
```

### HAVING 语法

对**聚合结果**进行过滤。引用输出列中的指标名：

```python
# 单个条件
having="total_amount > 1000"

# 多个条件
having="total_amount > 500 AND order_count > 2"
```

**不要**在 `having` 中传递 Jinja 模板、JSON 对象或双引号字符串。仅限纯 SQL 比较表达式。

### 排序

```python
order_by=["-total_amount"]   # 降序
order_by=["channel"]          # 升序
order_by=["-total_amount", "channel"]  # 多列
```

### 完整示例

```json
// "各渠道销售额"
{"workspace": "example", "metrics": ["total_amount"], "group_by": ["channel"]}

// "二月每日订单趋势"
{"workspace": "example", "metrics": ["order_count"], "group_by": ["order_date"],
 "where": "order_date >= '2026-02-01' AND order_date <= '2026-02-28'",
 "order_by": ["order_date"]}

// "销售额前 3 的渠道"
{"workspace": "example", "metrics": ["total_amount"], "group_by": ["channel"],
 "order_by": ["-total_amount"], "limit": 3}

// "已完成订单的渠道分布"
{"workspace": "example", "metrics": ["total_amount", "order_count"],
 "group_by": ["channel"], "where": "status = 'completed'"}

// "各品牌销售额，仅显示强势品牌"
{"workspace": "example", "metrics": ["total_amount"], "group_by": ["brand"],
 "order_by": ["-total_amount"], "having": "total_amount > 500"}
```

---

## 表概览（table_overview）

当用户询问"表的数据量 / 分区 / 最近更新时间 / 表健康状况"时使用。它读取 `information_schema`，返回每个表的元信息概览，不查询业务数据。

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|-------|------|----------|---------|-------------|
| `database` | string | 否 | `""` | 库名。为空时查询所有库（受白名单限制） |
| `tables` | list[string] | 否 | `[]` | 表名列表。裸表名需配合 `database`；也可用 `"db.table"` 全限定名 |
| `page_size` | int | 否 | `50` | 分页大小 |
| `page_token` | str | 否 | `""` | 分页游标，取上页 `meta.next_page_token` |

### 输出列

数据库、表名、表类型（BASE TABLE / VIEW 等）、最新更新分区、分区数量、创建时间、最近更新时间、总数据行数、总数据量(MB)、最近更新分区行数、最近更新分区数据量(MB)。按库名、表名排序。

### 示例

```json
// 某个库的所有表概览
{"database": "dw"}

// 指定表（裸名 + 库）
{"database": "dw", "tables": ["orders", "users"]}

// 指定表（全限定名，跨库）
{"tables": ["dw.orders", "ods.users"]}

// 全集群所有表（大集群上较慢，慎用）
{}
```

**规则：**
- 结果是统计元信息（来自 `information_schema`），行数为估算值，不代表精确计数。
- 不要用它来回答业务问题（如"销售额是多少"）--那是 `query_metric` 的职责。

---

## 何时使用原始 SQL（execute_query）

**仅有以下两种场景适合使用 `execute_query`：**

### 场景 A - 语义层不可用
`check_service_health` 返回的所有工作区均非 `status: "healthy"`。

### 场景 B - 无匹配指标
语义层是健康的，但 `list_metrics` 中没有与用户意图匹配的指标。

**关键规则 - 当语义层可以处理查询时，绝不要跳过它：**
- 如果 `check_service_health` 显示至少一个 `healthy` 工作区，且 `list_metrics` 有匹配的指标 -> **必须**使用 `query_metric`。不要编写原始 SQL。
- 仅当语义层确实无法满足时（上述场景 A 或 B），才回退到 `execute_query`。

**在以上任一场景下，按以下回退路径操作：**

### 回退路径：原始 SQL

```
list_databases()                              -> 查找数据库
list_tables(database="dw")                    -> 查找表
describe_table(database="dw", table="orders") -> 检查列
execute_query(sql="SELECT ... FROM dw.orders ...")
```

**使用原始 SQL 前务必警告用户：**

> "没有语义指标匹配你的查询。以下结果来自原始 SQL，可能存在聚合错误或重复计数，请谨慎使用。"

---

## 常见错误

| ❌ 不要 | ✅ 应该 |
|----------|------|
| 跳过 `get_query_guide` 或 `check_service_health` | 始终先调用它们--它们告诉你该使用哪个工作区 |
| 忘记 `workspace` 参数 | 每个语义工具都需要它 |
| 有可用指标时使用原始 SQL | `list_metrics` -> `query_metric` 始终优先 |
| 在检查维度之前调用 `query_metric` | 先用 `list_dimensions_for_metric` 验证 `group_by` 值 |
| 写 `having='{"x": 10}'`（JSON） | `having` 接受纯 SQL：`"x > 10"` |
| 用 `describe_table` 来规划指标查询 | 使用 `list_metrics`--指标会自动处理关联 |
| 将原始 SQL 结果当作权威结果 | 始终添加警告提示 |
