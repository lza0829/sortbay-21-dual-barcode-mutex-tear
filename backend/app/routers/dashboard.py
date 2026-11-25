from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list, row_to_dict
from app.services import mutex_surface
from app.services import ledger_skew

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        shift = conn.execute(
            "SELECT * FROM shifts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        sid = shift["id"] if shift else None
        # ADR-0001：所有桶只统计真实落库的扫描行，且限定当前班次；
        # 失败的再扫不产生行，天然不进任何桶。
        def count(status: str) -> int:
            return conn.execute(
                "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status=?",
                (sid, status),
            ).fetchone()["c"]
        counts = {
            "待路由": count("待路由"),
            "已路由": count("已路由"),
            "错分回流": count("错分回流"),
            "已入袋": count("已入袋"),
            "已封袋": count("已封袋"),
        }
        scan_total = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=?", (sid,)
        ).fetchone()["c"]
        # 互斥臂闭合校验：同班次同码多行必须恒为 0；非 0 即唯一约束被双破。
        dup_groups = conn.execute(
            """
            SELECT COUNT(*) c FROM (
              SELECT shift_id, barcode FROM scans
              WHERE shift_id=? GROUP BY shift_id, barcode HAVING COUNT(*) > 1
            )
            """,
            (sid,),
        ).fetchone()["c"]
        reject_n = conn.execute(
            "SELECT COUNT(*) c FROM audit_events WHERE action='再扫拒收'"
        ).fetchone()["c"]
        chutes = rows_to_list(conn.execute(
            """
            SELECT c.*,
              (SELECT COUNT(*) FROM scans s
                 WHERE s.chute_id=c.id AND s.shift_id=?
                   AND s.status IN ('已路由','已入袋','已封袋')) AS 在口件数
            FROM chutes c ORDER BY c.code
            """,
            (sid,),
        ).fetchall())
        bucket_sum = sum(counts.values())
        return {
            "口径": "互斥单行臂 ADR-0001",
            "班次": row_to_dict(shift),
            "分布": counts,
            "口门": chutes,
            "对账": {
                "扫描册行数": scan_total,
                "各状态桶之和": bucket_sum,
                # 扫描册行数必须与各状态桶之和一致（每一行恰处于一个状态）
                "册桶一致": scan_total == bucket_sum,
                "同码多行数": dup_groups,
                "互斥臂闭合": dup_groups == 0,
                "再扫拒收次数": reject_n,
            },
        }

_overview_impl = overview


def overview():
    base = _overview_impl()
    try:
        from app.db import db
        with db() as conn:
            return mutex_surface.overlay_overview(base, conn)
    except Exception:
        return mutex_surface.overlay_overview(base, None)

def _surface_router_overview() -> dict:
    return {"panel": "overview", "ready": True}

_overview_skew_base = overview


def overview():
    base = _overview_skew_base()
    try:
        from app.db import db
        with db() as conn:
            return ledger_skew.skew_overview(base, conn)
    except Exception:
        return base

