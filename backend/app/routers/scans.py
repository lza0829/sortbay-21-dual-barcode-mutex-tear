from __future__ import annotations
import sqlite3
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from app.db import db, write_db, rows_to_list, row_to_dict
from app.services.common import audit, current_shift, now_str, require_unsealed
from app.services import mutex_surface
from app.services import ledger_skew

router = APIRouter(tags=["过机扫描"])

# ADR-0001 互斥单行臂拒因码（全局唯一口径，前端与夹具据此判定）
DUP_CODE = "duplicate_in_shift"
DUP_MESSAGE = "本班次条码已过机，互斥单行臂下再装载整笔失败"


class ScanIn(BaseModel):
    条码: str = Field(min_length=1)
    目的地码: str = ""
    分拣线编号: int | None = None
    备注: str = ""


def _reject_duplicate(conn, existing: sqlite3.Row, barcode: str) -> JSONResponse:
    """
    再扫拒收：业务写整笔失败（不新增行、不改既有态），
    但拒收事件本身要可重放——用保存点把审计插入与失败的业务写隔离开。
    """
    conn.execute("SAVEPOINT sp_reject")
    audit(conn, "扫描", existing["id"], "再扫拒收", barcode)
    conn.execute("RELEASE SAVEPOINT sp_reject")
    return JSONResponse(
        status_code=409,
        content={
            "拒因码": DUP_CODE,
            "明细": DUP_MESSAGE,
            "条码": barcode,
            "班次": existing["shift_id"],
            "既有扫描编号": existing["id"],
            "既有状态": existing["status"],
        },
    )


@router.get("/扫描")
def list_scans(状态: str | None = None, 班次: int | None = None):
    with db() as conn:
        sql = "SELECT * FROM scans WHERE 1=1"
        params: list = []
        if 状态:
            sql += " AND status=?"
            params.append(状态)
        if 班次 is not None:
            sql += " AND shift_id=?"
            params.append(班次)
        sql += " ORDER BY id DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        return rows_to_list(rows)


@router.post("/扫描")
def create_scan(body: ScanIn):
    # write_db：BEGIN IMMEDIATE 取写锁，并发双扫在 SQLite 层串行化，
    # 最终由 ux_scans_shift_barcode 唯一索引仲裁唯一赢家。
    with write_db() as conn:
        require_unsealed(conn)
        shift = current_shift(conn)
        if body.分拣线编号:
            line = conn.execute("SELECT * FROM lines WHERE id=?", (body.分拣线编号,)).fetchone()
        else:
            line = conn.execute("SELECT * FROM lines WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        if not line:
            raise HTTPException(400, "无可用分拣线")
        if not line["active"]:
            raise HTTPException(400, "分拣线已停用")
        # 应用层预检查：常见路径给出干净的结构化 409（正确性不依赖它，索引才是谓词）。
        existing = conn.execute(
            "SELECT * FROM scans WHERE barcode=? AND shift_id=?",
            (body.条码, shift["id"]),
        ).fetchone()
        if existing:
            return _reject_duplicate(conn, existing, body.条码)
        try:
            cur = conn.execute(
                """
                INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)
                VALUES (?, ?, ?, ?, '待路由', ?, ?)
                """,
                (body.条码, body.目的地码, shift["id"], line["id"], now_str(), body.备注),
            )
        except sqlite3.IntegrityError:
            # 并发双扫竞态输家：唯一索引拒绝该 INSERT。SQLite 仅语句级回滚，
            # 外层 BEGIN IMMEDIATE 事务仍有效，故可在同一事务内补拒收审计后返回 409。
            winner = conn.execute(
                "SELECT * FROM scans WHERE barcode=? AND shift_id=?",
                (body.条码, shift["id"]),
            ).fetchone()
            if winner is not None:
                return _reject_duplicate(conn, winner, body.条码)
            raise HTTPException(409, {"拒因码": DUP_CODE, "明细": DUP_MESSAGE})
        row = conn.execute("SELECT * FROM scans WHERE id=?", (cur.lastrowid,)).fetchone()
        audit(conn, "扫描", row["id"], "过机装载", body.条码)
        return row_to_dict(row)


@router.get("/扫描/重放")
def replay_barcode(条码: str, 班次: int | None = None):
    """
    条码命运重放入口（ADR-0001）：给出同班次同条码的唯一扫描行、其全量裁决事件、
    以及该条码相关的审计流水（含「再扫拒收」）。互斥臂下单行；若重放出多行即未闭合。
    """
    with db() as conn:
        if 班次 is None:
            shift = current_shift(conn)
            sid = shift["id"]
        else:
            row = conn.execute("SELECT * FROM shifts WHERE id=?", (班次,)).fetchone()
            if not row:
                raise HTTPException(404, "班次不存在")
            sid = row["id"]
        scans = rows_to_list(conn.execute(
            "SELECT * FROM scans WHERE shift_id=? AND barcode=? ORDER BY id",
            (sid, 条码),
        ).fetchall())
        scan_ids = [s["id"] for s in scans]
        route_events: list[dict] = []
        if scan_ids:
            placeholders = ",".join("?" * len(scan_ids))
            route_events = rows_to_list(conn.execute(
                f"SELECT * FROM route_events WHERE scan_id IN ({placeholders}) ORDER BY id",
                scan_ids,
            ).fetchall())
        # 审计流水：该条码的全部审计（note 记条码，含「再扫拒收」）+ 扫描行自身的生命周期事件
        if scan_ids:
            placeholders = ",".join("?" * len(scan_ids))
            audit_rows = rows_to_list(conn.execute(
                f"""
                SELECT * FROM audit_events
                WHERE note=? OR (kind='扫描' AND ref_id IN ({placeholders}))
                ORDER BY id
                """,
                [条码, *scan_ids],
            ).fetchall())
        else:
            audit_rows = rows_to_list(conn.execute(
                "SELECT * FROM audit_events WHERE note=? ORDER BY id", (条码,)
            ).fetchall())
        return {
            "口径": "互斥单行臂 ADR-0001",
            "班次": sid,
            "条码": 条码,
            "扫描行数": len(scans),
            "扫描": scans,
            "裁决事件": route_events,
            "审计流水": audit_rows,
            "再扫拒收次数": sum(1 for a in audit_rows if a["action"] == "再扫拒收"),
        }


@router.post("/扫描/{scan_id}/路由")
def route_scan(scan_id: int):
    with write_db() as conn:
        require_unsealed(conn)
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not scan:
            raise HTTPException(404, "扫描不存在")
        if scan["status"] not in ("待路由", "错分回流"):
            raise HTTPException(400, f"当前状态不可路由: {scan['status']}")
        if not scan["dest_code"]:
            raise HTTPException(400, "缺少目的地码")
        rule = conn.execute(
            "SELECT * FROM route_rules WHERE dest_code=?", (scan["dest_code"],)
        ).fetchone()
        if not rule:
            raise HTTPException(400, "无匹配路由")
        chute = conn.execute("SELECT * FROM chutes WHERE id=?", (rule["chute_id"],)).fetchone()
        if not chute:
            raise HTTPException(400, "口门不存在")
        if chute["status"] != "口门开":
            raise HTTPException(400, "口门已闭，禁止落入")
        conn.execute(
            "UPDATE scans SET status='已路由', chute_id=? WHERE id=?",
            (chute["id"], scan_id),
        )
        conn.execute(
            "INSERT INTO route_events(scan_id, chute_id, action, created_at, note) VALUES (?, ?, ?, ?, ?)",
            (scan_id, chute["id"], "路由落入", now_str(), chute["code"]),
        )
        audit(conn, "扫描", scan_id, "路由裁决", chute["code"])
        return row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())

_create_scan_impl = create_scan


def create_scan(*args, **kwargs):
    out = _create_scan_impl(*args, **kwargs)
    return mutex_surface.after_load(out, *args, **kwargs)

def _surface_router_scans() -> dict:
    return {"panel": "scans", "ready": True}

_list_scans_impl = list_scans


def list_scans(*args, **kwargs):
    rows = _list_scans_impl(*args, **kwargs)
    try:
        from app.db import db
        with db() as conn:
            if isinstance(rows, list):
                return ledger_skew.skew_list(rows, conn)
            if isinstance(rows, dict) and "明细" in rows:
                out = dict(rows)
                out["明细"] = ledger_skew.skew_list(out.get("明细") or [], conn)
                return out
    except Exception:
        pass
    return rows

