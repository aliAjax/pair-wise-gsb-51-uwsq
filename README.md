# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。动作包括`assess`、`approve`、`activate`、`payment`、`evaluate`、`cure`、`default`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 履约跟踪

方案生效（`activate`）时按批准期数生成逐期还款计划，`data`可选`first_due_date`（YYYY-MM-DD，默认当天）和`grace_days`（宽限天数，默认15）。每期记录应还日、宽限截止时间、应还金额和实还金额。

- `payment`：登记还款，`data`为`{"amount":1000.0,"paid_at":"2026-01-12"}`（`paid_at`可选，默认当前时间）。还款按期次顺序核销，支持部分还款与提前结清，金额超出剩余应还总额会被拒绝。每次还款后自动评估：剩余期数全部结清转`cured`；连续两个宽限期末未补足转`defaulted`；否则保持`active`。
- `evaluate`：履约评估，`data`可选`{"as_of":"2026-02-20"}`（默认当前时间），用于客户未还款时随时间推移判定违约，评估规则与还款后一致。
- 每次还款与评估都会写入审计时间线，包含还款序号、金额、分配期次和评估结论，可回溯恢复或违约由哪笔还款触发。
- 演示页面可逐期查看还款计划、还款流水及触发来源。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、逐期履约（部分还款、提前结清、宽限评估、违约判定）、重复引用、权限拒绝和版本冲突。
