from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db, write_db, rows_to_list, row_to_dict
from app.services.common import audit, now_str

router = APIRouter(tags=["班次"])


class ShiftIn(BaseModel):
    营业日: str = Field(min_length=8)
    班次名: str = Field(min_length=1)


@router.get("/班次")
def list_shifts():
    with db() as conn:
        return rows_to_list(conn.execute("SELECT * FROM shifts ORDER BY id DESC").fetchall())


@router.get("/班次/对账")
def reconcile(shift_id: int | None = None):
    with db() as conn:
        if shift_id is None:
            shift = conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone()
        else:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")
        sid = shift["id"]
        scan_n = conn.execute("SELECT COUNT(*) c FROM scans WHERE shift_id=?", (sid,)).fetchone()["c"]
        routed = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('已路由','已入袋','已封袋')",
            (sid,),
        ).fetchone()["c"]
        bag_n = conn.execute("SELECT COUNT(*) c FROM bags WHERE shift_id=?", (sid,)).fetchone()["c"]
        sealed_bag = conn.execute(
            "SELECT COUNT(*) c FROM bags WHERE shift_id=? AND status='已封袋'", (sid,)
        ).fetchone()["c"]
        divert_n = conn.execute(
            "SELECT COUNT(*) c FROM divert_batches WHERE shift_id=?", (sid,)
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('待路由','错分回流')",
            (sid,),
        ).fetchone()["c"]
        # ADR-0001 互斥单行臂：同班次同码多行恒为 0，否则视为未闭合。
        dup_groups = conn.execute(
            """
            SELECT COUNT(*) c FROM (
              SELECT barcode FROM scans WHERE shift_id=?
              GROUP BY barcode HAVING COUNT(*) > 1
            )
            """,
            (sid,),
        ).fetchone()["c"]
        reject_n = conn.execute(
            """
            SELECT COUNT(*) c FROM audit_events
            WHERE action='再扫拒收' AND ref_id IN (
              SELECT id FROM scans WHERE shift_id=?
            )
            """,
            (sid,),
        ).fetchone()["c"]
        return {
            "口径": "互斥单行臂 ADR-0001",
            "班次": row_to_dict(shift),
            "扫描总数": scan_n,
            "已路由及以上": routed,
            "袋数": bag_n,
            "已封袋数": sealed_bag,
            "回流批次数": divert_n,
            "未清件数": pending,
            "同码多行数": dup_groups,
            "互斥臂闭合": dup_groups == 0,
            "再扫拒收次数": reject_n,
        }


@router.post("/班次")
def create_shift(body: ShiftIn):
    with write_db() as conn:
        open_one = conn.execute("SELECT id FROM shifts WHERE sealed=0").fetchone()
        if open_one:
            raise HTTPException(400, "仍有未封班班次，先封班再开新班")
        try:
            conn.execute(
                "INSERT INTO shifts(business_day, name, sealed) VALUES (?, ?, 0)",
                (body.营业日, body.班次名),
            )
        except Exception as e:
            raise HTTPException(400, f"开班失败: {e}")
        row = conn.execute(
            "SELECT * FROM shifts WHERE business_day=? AND name=?",
            (body.营业日, body.班次名),
        ).fetchone()
        audit(conn, "班次", row["id"], "开班", body.班次名)
        return row_to_dict(row)


@router.post("/班次/{shift_id}/封班")
def seal_shift(shift_id: int):
    with write_db() as conn:
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")
        if shift["sealed"]:
            raise HTTPException(400, "已封班")
        pending = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('待路由','错分回流','已路由','已入袋')",
            (shift_id,),
        ).fetchone()["c"]
        if pending:
            raise HTTPException(400, f"仍有未封袋/未路由件 {pending}，禁止封班")
        conn.execute(
            "UPDATE shifts SET sealed=1, sealed_at=? WHERE id=?",
            (now_str(), shift_id),
        )
        audit(conn, "班次", shift_id, "封班", "")
        return row_to_dict(conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone())

def _surface_router_shifts() -> dict:
    return {"panel": "shifts", "ready": True}

