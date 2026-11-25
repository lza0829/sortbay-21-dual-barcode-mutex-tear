# 分拣过机台（Sortbay）

骨架：过机扫描装载 → 口门路由裁决 → 错分回流批次 → 封袋班次对账

- 前端 http://127.0.0.1:6600
- API http://127.0.0.1:11600

## 同班次同条码再扫口径（ADR-0001）

全局贯彻**互斥单行臂**（见 `docs/adr-0001-same-shift-barcode-mutex.md`）：

- 同一班次同一条码在 `scans` 中至多一行，由库级唯一索引
  `ux_scans_shift_barcode(shift_id, barcode)` 仲裁，应用层预检仅为优化。
- 第二次过机装载**整笔失败**：返回 `409` 与拒因码 `duplicate_in_shift`，
  不新增行、不改既有态；失败再扫不进任何读路径（待路由桶/总览/对账）。
- 拒收事件以「再扫拒收」落 `audit_events`（保存点隔离，可重放），
  条码命运重放入口：`GET /api/扫描/重放?条码=...`。
- 并发双扫：写连接 WAL + `busy_timeout` + `BEGIN IMMEDIATE` 串行化，
  唯一索引保证恰好一个赢家；半截回滚后同码行数仍为 1。
- 仅覆盖同班次同条码；跨班次同条码不互斥，策略另立需求。

## 测试

```bash
cd backend
python -m pytest          # 10 个夹具，含并发双扫/脏数据迁移/重放
```
