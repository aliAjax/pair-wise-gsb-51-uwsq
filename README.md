# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值、分期履约判定和冲突检查。
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
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

业务动作：`assess`（评估）、`approve`（批准）、`activate`（生效）、`pay`（登记还款）、`evaluate`（履约评估）、`cure`（人工恢复）、`default`（人工标记违约）。`pay`/`evaluate`由`servicer`执行。

## 分期履约规则

`activate`时按批准期数生成分期计划（数据中可带`first_due_date`，默认下月今日；`grace_days`默认10天）。每期记录：

- `period`期次、`due_amount`每期应还、`paid_amount`累计实还；
- `due_date`应还日、`grace_deadline`宽限截止时间；
- `status`：`pending`待还 / `partial`部分还款 / `paid`已结清。

判定逻辑：

- `pay`（数据：`amount`金额，`paid_at`可选日期）：还款按期次顺序冲抵最早未结清期，支持提前补缴和部分还款；超过剩余应还总额将被拒绝。
- 剩余期数全部结清 → 自动转`cured`恢复，`payload.cured_by_payment`记录触发恢复的还款编号。
- 宽限期内（或仅错过1个宽限期）仍有欠款 → 保持`active`。
- `evaluate`（数据：`as_of`评估日期，默认今天）：出现连续两个宽限截止日结束仍未补足的期次 → 自动转`defaulted`，`payload.defaulted_periods`记录对应期次；中途补缴会打断连续计数。
- 每笔还款（含各期冲抵明细）和每次状态变化都写入审计时间线，`GET /api/records/{id}/audit`可逐笔追溯恢复或违约由哪笔还款触发。

演示页支持输入记录ID逐期查看应还/实还/剩余/宽限截止/结清来源、登记还款、按日期评估，并展示标注触发事件的时间线。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、分期还款冲抵、提前结清恢复、连续宽限违约、重复引用、权限拒绝和版本冲突。
