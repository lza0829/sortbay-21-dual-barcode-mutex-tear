from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list, row_to_dict
from app.services.mutex_probe import mutex_probe

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
        # 互斥探针：占用灯/对账结论只由真实扫描行与同事务审计派生（单一证据面）。
        probe = mutex_probe(conn, sid) if sid is not None else None
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
                "同码多行数": probe["同码多行数"] if probe else 0,
                "互斥臂闭合": probe["互斥臂闭合"] if probe else True,
                "皮带双挂数": probe["皮带双挂数"] if probe else 0,
                "无行成功灯数": probe["无行成功灯数"] if probe else 0,
                "成功灯数": probe["成功灯数"] if probe else 0,
                "灯册一致": probe["灯册一致"] if probe else True,
                "再扫拒收次数": reject_n,
            },
            "互斥探针": probe,
        }


@router.get("/互斥探针")
def probe(班次: int | None = None):
    """
    互斥证据定位面（占用探针）：把同班次条码占用、扫描册、成功灯、皮带钉、
    调度桶钉在同一对照面上。任何计数非零即互斥撕裂，给出可核明细用于重放对账。
    """
    with db() as conn:
        if 班次 is None:
            shift = conn.execute(
                "SELECT * FROM shifts ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (班次,)).fetchone()
        if not shift:
            from fastapi import HTTPException
            raise HTTPException(404, "班次不存在")
        return mutex_probe(conn, shift["id"])
