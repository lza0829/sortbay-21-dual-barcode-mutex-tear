"""
21 事故「同班次同条码假双破」收口夹具（ADR-0001 互斥单行臂）。

全局唯一口径：任一成功灯必须能由同班次同条码至多一行正式扫描行 ∪ 扫描册 ∪
占用态（互斥探针）同命运重放。装载写口、扫描册、班次边界、占用探针、调度页、
审计流水必须同刷到同一结局；旁路改提示不算修完。

覆盖事故夹具：
  A. 双回执绿双行 —— 两路同码装载，只允许一盏绿灯、册内一行，全读路径收敛。
  B. 后到回执绿却无行 —— 无行成功灯必须被探针识破，且写闸不产生该灯。
  C. 半截一行幽灵 —— 并发半截回滚后，一行钉住、幽灵行不得存在，灯册同命运。
  D. 调度双涨占用单钉 —— 调度桶之和等于册行数；占用灯每码恒一行；皮带不双挂。
另含表面层清除与皮带钉双挂识破。
"""
from __future__ import annotations

import threading

from fastapi.responses import JSONResponse

from app.db import connect

BARCODE = "PKG-TEAR-21"


def _load(client, barcode=BARCODE, dest="110"):
    return client.post("/扫描", json={"条码": barcode, "目的地码": dest})


def _book(client, barcode=BARCODE):
    return [r for r in client.get("/扫描").json() if r["barcode"] == barcode]


def _probe(client):
    return client.get("/互斥探针").json()


# ---------------------------------------------------------------------------
# A. 双回执绿双行：同码连装两路，恰一绿一拒，全链路收敛到单行结局
# ---------------------------------------------------------------------------
def test_dual_green_receipts_converge_to_single_row(client):
    r1 = _load(client)
    r2 = _load(client)
    assert r1.status_code == 200
    assert r2.status_code == 409
    assert r2.json()["拒因码"] == "duplicate_in_shift"

    # 扫描册：同班次同码恰好一行
    book = _book(client)
    assert len(book) == 1
    first = r1.json()
    assert book[0]["id"] == first["id"]

    # 审计流水：一盏过机装载绿灯 + 一次再扫拒收，双路回执同刷可重放
    audit_rows = client.get("/审计", params={"条码": BARCODE}).json()
    assert [a["action"] for a in audit_rows].count("过机装载") == 1
    assert [a["action"] for a in audit_rows].count("再扫拒收") == 1

    # 总览/班次对账/占用探针三处必须收敛到同一结局
    ov = client.get("/总览").json()
    rec = client.get("/班次/对账").json()
    probe = _probe(client)
    for face in (ov["对账"], rec, probe):
        assert face["互斥臂闭合"] is True
        assert face["同码多行数"] == 0
        assert face["灯册一致"] is True
    assert probe["成功灯数"] == probe["扫描册行数"]
    assert probe["无行成功灯数"] == 0
    # 重放入口：单行、单灯、单拒收，结局确定
    replay = client.get("/扫描/重放", params={"条码": BARCODE}).json()
    assert replay["扫描行数"] == 1
    assert replay["互斥臂闭合"] is True


# ---------------------------------------------------------------------------
# B. 后到回执绿却无行：写闸永不产生无行绿灯；人为脏灯须被探针识破
# ---------------------------------------------------------------------------
def test_green_receipt_without_row_is_detected_and_never_produced(client):
    # 正常写闸：后到的那一路拿的是 409，不是绿灯，因此不存在「绿却无行」
    assert _load(client).status_code == 200
    late = _load(client)
    assert late.status_code == 409
    assert _probe(client)["无行成功灯数"] == 0

    # 人为制造一盏引用已不存在行的成功灯（模拟历史假双破残留），
    # 探针必须在同一对照面上识破并判未闭合。
    raw = connect()
    raw.execute("BEGIN IMMEDIATE")
    ghost_ref = raw.execute("SELECT MAX(id) m FROM scans").fetchone()["m"] + 999
    raw.execute(
        "INSERT INTO audit_events(kind, ref_id, action, created_at, note) "
        "VALUES ('扫描', ?, '过机装载', '2026-11-25 09:00:00', ?)",
        (ghost_ref, BARCODE),
    )
    raw.execute("COMMIT")
    raw.close()

    probe = _probe(client)
    assert probe["互斥臂闭合"] is False
    assert probe["无行成功灯数"] == 1
    assert probe["无行成功灯明细"][0]["barcode"] == BARCODE
    # 总览与班次对账同步翻未闭合（同一证据面，不允许各说各话）
    assert client.get("/总览").json()["对账"]["互斥臂闭合"] is False
    assert client.get("/班次/对账").json()["互斥臂闭合"] is False


# ---------------------------------------------------------------------------
# C. 半截一行幽灵：并发双装走唯一写闸，回滚后一行钉住、无幽灵、灯册同命运
# ---------------------------------------------------------------------------
def test_half_commit_leaves_one_pinned_row_no_ghost(client):
    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            from app.db import write_db
            from app.routers.scans import load_scan, ScanIn
            barrier.wait()
            with write_db() as conn:
                out = load_scan(conn, ScanIn(条码=BARCODE, 目的地码="110"))
                results.append(
                    "rejected" if isinstance(out, JSONResponse) and out.status_code == 409
                    else "committed"
                )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(20); t2.join(20)
    assert not errors, errors
    assert sorted(results) == ["committed", "rejected"]

    # 一行钉住，幽灵行不存在
    book = _book(client)
    assert len(book) == 1
    sid = client.get("/总览").json()["班次"]["id"]
    raw = connect()
    n = raw.execute(
        "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND barcode=?", (sid, BARCODE)
    ).fetchone()["c"]
    raw.close()
    assert n == 1

    probe = _probe(client)
    assert probe["互斥臂闭合"] is True
    assert probe["同码多行数"] == 0
    assert probe["无行成功灯数"] == 0
    assert probe["灯册不符数"] == 0
    # 该码占用灯恒为一行
    occ = next(o for o in probe["占用灯"] if o["barcode"] == BARCODE)
    assert occ["占用行数"] == 1


# ---------------------------------------------------------------------------
# D. 调度双涨占用单钉：桶之和=册行数；每码占用一行；皮带钉不双挂
# ---------------------------------------------------------------------------
def test_dispatch_counts_single_occupancy_and_belt_peg(client):
    # 同码连装三次：仅一行落库，两次拒收，调度计数不得双涨
    assert _load(client).status_code == 200
    assert _load(client).status_code == 409
    assert _load(client).status_code == 409

    ov = client.get("/总览").json()
    assert ov["分布"]["待路由"] == 1
    assert ov["对账"]["扫描册行数"] == 1
    assert ov["对账"]["各状态桶之和"] == 1
    assert ov["对账"]["册桶一致"] is True

    probe = _probe(client)
    assert probe["桶册一致"] is True
    assert probe["调度桶之和"] == probe["扫描册行数"] == 1
    # 占用单钉：该码只被一条正式行占用
    occ = [o for o in probe["占用灯"] if o["barcode"] == BARCODE]
    assert len(occ) == 1 and occ[0]["占用行数"] == 1
    # 皮带钉不双挂
    assert probe["皮带双挂数"] == 0


def test_belt_double_peg_is_exposed_when_dirty(client):
    """皮带钉若双挂（历史脏数据：同线同班同码两行），探针必须识破并判未闭合。"""
    sid = client.get("/总览").json()["班次"]["id"]
    raw = connect()
    raw.execute("DROP INDEX ux_scans_shift_barcode")
    line_id = raw.execute("SELECT id FROM lines ORDER BY id LIMIT 1").fetchone()["id"]
    raw.execute(
        "INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)"
        " VALUES (?, '110', ?, ?, '待路由', '2026-11-25 08:00:00', '皮带挂一')",
        (BARCODE, sid, line_id),
    )
    raw.execute(
        "INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)"
        " VALUES (?, '110', ?, ?, '待路由', '2026-11-25 08:01:00', '皮带挂二')",
        (BARCODE, sid, line_id),
    )
    raw.commit()
    raw.close()

    probe = _probe(client)
    assert probe["互斥臂闭合"] is False
    assert probe["同码多行数"] == 1
    assert probe["皮带双挂数"] == 1
    peg = probe["皮带双挂明细"][0]
    assert peg["barcode"] == BARCODE and peg["n"] == 2

    # 重跑初始化迁移即收口：归并为单行、重建唯一索引、探针恢复闭合
    from app.db import init_db
    init_db()
    probe2 = _probe(client)
    assert probe2["同码多行数"] == 0
    assert probe2["皮带双挂数"] == 0
    assert probe2["互斥臂闭合"] is True
    assert len(_book(client)) == 1


# ---------------------------------------------------------------------------
# 表面层（第二路语义）必须彻底清除：任何响应不得携带 surface_* 旁路字段
# ---------------------------------------------------------------------------
def test_no_surface_parasite_fields_anywhere(client):
    _load(client)
    for path in ("/总览", "/扫描", "/班次/对账", "/互斥探针", "/审计"):
        body = client.get(path).json()
        text = str(body)
        assert "surface_ok" not in text
        assert "surface_probe" not in text
        assert "surface_last" not in text
        assert "mutex_twin" not in text
        assert "并装" not in text

    # 库里不得残留任何假灯设置键
    raw = connect()
    keys = [r["key"] for r in raw.execute("SELECT key FROM settings").fetchall()]
    raw.close()
    assert not any(k.startswith(("surface_", "mutex_twin:", "mutex_surface:")) for k in keys)
    # 审计不得出现旁路「成功宣称」
    actions = [a["action"] for a in client.get("/审计").json()]
    assert "成功宣称" not in actions
