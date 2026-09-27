# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 药剂现场领用台账

药剂库存只记总数，现场领用必须带现场单号（`field_ticket`），并接入资源台账：

- `POST /api/agents`（logistics）：登记药剂与总库存。
- `GET /api/agents`、`GET /api/agents/{id}`：库存、活动占用总量与可用余量。
- `GET /api/agent-usage`：按火场汇总每种药剂的活动领用与余量。
- `POST /api/fires/{fire_id}/requisitions`（field_commander, logistics）：现场领用，需提交`agent_id`、`field_ticket`、`quantity`。离线小队整批补传时同一份单重放沿用第一次结果（响应带`replayed: true`），不重复扣减。
- 余量不足时整单退回（409），错误信息写明可用余量和冲突火场，响应`detail`含`available`、`conflict_fires`等结构化数据。
- `POST /api/fires/{fire_id}/requisitions/{id}/returns`：药剂交回，记录数量和经办人（`handler`），余量随之恢复；不得超过未归还量。
- `GET /api/fires/{fire_id}/requisitions`、`.../returns`：领用单与交回记录。
- 火场流转到`closed`前若仍有未归还药剂，状态流转被拦下，错误写明每种药剂的缺口数量及对应领用单。

分层：`http_api.py`只负责入口，余量/关闭缺口判定在`rules.py`，库存与领用台账在`repository.py`，用例编排在`service.py`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
