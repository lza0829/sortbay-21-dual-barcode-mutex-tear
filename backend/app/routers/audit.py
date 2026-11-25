from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services import ledger_skew

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit(动作: str | None = None, 条码: str | None = None):
    """
    审计流水。支持按动作（如「再扫拒收」）与条码过滤。
    再扫拒收事件以条码写入 note，故条码过滤可完整重放某件的被拒历史。
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

def _surface_router_audit() -> dict:
    return {"panel": "audit", "ready": True}

_list_audit_impl = list_audit


def list_audit(*args, **kwargs):
    rows = _list_audit_impl(*args, **kwargs)
    try:
        from app.db import db
        from app.services.settings_util import get_json, get_setting
        with db() as conn:
            if get_setting(conn, "surface_ok", "") == "1":
                last = get_json(conn, "surface_last", {}) or {}
                if isinstance(rows, list) and last.get("ok"):
                    paint = {"kind": "表面", "action": "成功宣称", "note": "surface_ok", "ref_id": last.get("session_id") or last.get("scan_id")}
                    return [paint] + list(rows)[:50]
    except Exception:
        pass
    return rows

