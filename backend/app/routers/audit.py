from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit(动作: str | None = None, 条码: str | None = None):
    """
    审计流水。支持按动作（如「再扫拒收」）与条码过滤。
    再扫拒收事件以条码写入 note，故条码过滤可完整重放某件的被拒历史。
    审计只写真实业务事件，禁止任何旁路「成功宣称」注入。
    """
    with db() as conn:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: list = []
        if 动作:
            sql += " AND action=?"
            params.append(动作)
        if 条码:
            sql += " AND note=?"
            params.append(条码)
        sql += " ORDER BY id DESC LIMIT 200"
        return rows_to_list(conn.execute(sql, params).fetchall())
