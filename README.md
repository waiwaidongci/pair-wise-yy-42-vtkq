# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。药剂库存接入资源台账：小队离线领用后整批补传，按现场单号幂等，按火场汇总余量，交回恢复库存，未归还时阻止火场关闭。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量，以及药剂领用/退回的判定（余量裁定、冲突说明、关闭缺口）。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链；药剂、领用批次、交回记录台账，领用在锁内完成"查余量+扣减"保证并发不超发。
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
- `POST /api/chemicals`，登记药剂与总库存（logistics）
- `GET /api/chemicals`，按火场汇总活动领用与余量
- `POST /api/items/{id}/requisitions`，现场领用，必带`field_ref`现场单号；重放返回首次结果（HTTP 200且`replayed=true`，不重复扣减）
- `GET /api/items/{id}/requisitions`
- `POST /api/requisitions/{id}/returns`，交回药剂，记录`quantity`与`handler`经办人，余量随之恢复
- `GET /api/requisitions/{id}/returns`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。领用药剂超出余量时退回并说明冲突火场及其占用量；火场关闭前仍有未归还药剂时状态流转停下并写明缺口（药剂名与未归还数量）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
