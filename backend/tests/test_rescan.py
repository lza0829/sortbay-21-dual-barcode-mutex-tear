"""
ADR-0001 互斥单行臂夹具。

覆盖需求指定的全部场景：
  1. 同码连扫两次（第二次整笔失败：409、无新行、既有态不变、不入待路由桶）
  2. 并发双扫半截（唯一索引仲裁，恰好一行；回滚后行数可由口径重放）
  3. 互斥失败脏写（拒收不留半截行/状态改动/幽灵待路由）
  4. 另开臂下总览漏一行后再裁决 —— 在本全局口径下结构性不可能（同码多行恒为 0）
  5. 同码两行抢同一口门 —— 互斥臂下结构性不可能
  6. 跨班次同条码可再扫（唯一约束含 shift_id，跨班次策略不在本需求内）
  7. 条码命运重放（扫描册行数、裁决事件、拒收审计一致可重放）
"""
from __future__ import annotations

import threading

import pytest

from app.db import connect


BARCODE = "PKG-DUP-0001"


def _scan(client, barcode=BARCODE, dest="110"):
    return client.post("/扫描", json={"条码": barcode, "目的地码": dest})


def _current_shift_id(client):
    return client.get("/总览").json()["班次"]["id"]


def _scan_book(client, barcode=BARCODE, sid=None):
    rows = client.get("/扫描").json()
    rows = [r for r in rows if r["barcode"] == barcode]
    if sid is not None:
        rows = [r for r in rows if r["shift_id"] == sid]
    return rows


def test_same_barcode_scanned_twice_second_load_fails_entirely(client):
    """同码连扫两次：互斥单行——第二次整笔失败，不新增行、不改既有态。"""
    r1 = _scan(client)
    assert r1.status_code == 200
    first = r1.json()

    r2 = _scan(client)
    # 结构化回执：409 + 拒因码 + 既有行引用
    assert r2.status_code == 409
    body = r2.json()
    assert body["拒因码"] == "duplicate_in_shift"
    assert body["既有扫描编号"] == first["id"]
    assert body["既有状态"] == "待路由"
    assert body["班次"] == first["shift_id"]

    # 扫描册行数：仍为 1，没有第二行
    book = _scan_book(client)
    assert len(book) == 1
    assert book[0]["id"] == first["id"]
    # 既有行状态原样，未被第二次装载触碰
    assert book[0]["status"] == "待路由"
    assert book[0]["chute_id"] is None

    # 待路由桶：失败的再扫不进桶（桶里只有首次那 1 件，不是 2 件）
    overview = client.get("/总览").json()
    assert overview["分布"]["待路由"] == 1
    assert overview["对账"]["扫描册行数"] == 1
    assert overview["对账"]["同码多行数"] == 0
    assert overview["对账"]["互斥臂闭合"] is True
    assert overview["对账"]["再扫拒收次数"] == 1

    # 审计：一次过机装载 + 一次再扫拒收，拒收可重放
    audit_rows = client.get("/审计", params={"条码": BARCODE}).json()
    actions = [a["action"] for a in audit_rows]
    assert "过机装载" in actions
    assert "再扫拒收" in actions


def test_concurrent_double_scan_half_commit_single_winner(client):
    """
    并发双扫半截：两个线程同时装载同码。
    WAL + BEGIN IMMEDIATE 串行化写锁，ux_scans_shift_barcode 仲裁唯一赢家：
    恰好一个 200、一个 409；库内同班次同码恰好一行，无双破唯一。
    """
    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            conn = connect()
            from app.db import write_db
            from app.services.common import require_unsealed, current_shift, now_str
            barrier.wait()  # 尽量让两个事务同时撞上
            with write_db() as conn:
                require_unsealed(conn)
                shift = current_shift(conn)
                line = conn.execute(
                    "SELECT * FROM lines WHERE active=1 ORDER BY id LIMIT 1"
                ).fetchone()
                # 模拟装载入口主体：不做应用层预检，直接 INSERT，压测唯一索引仲裁
                try:
                    conn.execute(
                        """
                        INSERT INTO scans(barcode, dest_code, shift_id, line_id,
                                          status, scanned_at, note)
                        VALUES (?, ?, ?, ?, '待路由', ?, '')
                        """,
                        (BARCODE, "110", shift["id"], line["id"], now_str()),
                    )
                    results.append("committed")
                except Exception as e:  # noqa: BLE001
                    results.append(f"rejected:{type(e).__name__}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(15); t2.join(15)
    assert not errors, errors

    # 恰好一个赢家
    assert sorted(results) == ["committed", "rejected:IntegrityError"]

    # 半截回滚后同码行数为 1 —— 由扫描册行数重放
    book = _scan_book(client)
    assert len(book) == 1
    sid = _current_shift_id(client)
    # 库级谓词直查：同班次同码唯一
    raw = connect()
    n = raw.execute(
        "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND barcode=?",
        (sid, BARCODE),
    ).fetchone()["c"]
    raw.close()
    assert n == 1

    overview = client.get("/总览").json()
    assert overview["对账"]["同码多行数"] == 0
    assert overview["对账"]["互斥臂闭合"] is True


def test_rejected_rescan_leaves_no_dirty_write(client):
    """互斥失败脏写：拒收后无半截行、无状态改动、无幽灵待路由、无裁决事件。"""
    first = _scan(client).json()
    before = client.get("/扫描").json()
    assert len([r for r in before if r["barcode"] == BARCODE]) == 1

    rejected = _scan(client)
    assert rejected.status_code == 409

    after = client.get("/扫描").json()
    same = [r for r in after if r["barcode"] == BARCODE]
    assert len(same) == 1
    assert same[0] == first  # 既有行整行未被修改

    # 没有因拒收产生任何路由事件
    replay = client.get("/扫描/重放", params={"条码": BARCODE}).json()
    assert replay["扫描行数"] == 1
    assert replay["裁决事件"] == []
    assert replay["再扫拒收次数"] == 1

    # 班次对账：扫描总数不包含失败再扫
    rec = client.get("/班次/对账").json()
    assert rec["扫描总数"] == 1
    assert rec["未清件数"] == 1
    assert rec["同码多行数"] == 0
    assert rec["互斥臂闭合"] is True
    assert rec["再扫拒收次数"] == 1


def test_rescan_after_routed_still_rejected_and_state_untouched(client):
    """路由裁决后再扫仍拒：第一次已落入口门，第二次不得装载、不得改裁决结果。"""
    first = _scan(client).json()
    routed = client.post(f"/扫描/{first['id']}/路由")
    assert routed.status_code == 200
    chute_after_route = routed.json()["chute_id"]
    assert routed.json()["status"] == "已路由"

    again = _scan(client)
    assert again.status_code == 409
    assert again.json()["既有状态"] == "已路由"

    book = _scan_book(client)
    assert len(book) == 1
    assert book[0]["status"] == "已路由"
    assert book[0]["chute_id"] == chute_after_route

    # 口门页：在口件数仍为 1，拒扫没有抢入口门
    chutes = client.get("/口门").json()
    target = next(c for c in chutes if c["id"] == chute_after_route)
    overview = client.get("/总览").json()
    oc = next(c for c in overview["口门"] if c["id"] == chute_after_route)
    assert oc["在口件数"] == 1


def test_two_same_code_rows_grabbing_same_chute_structurally_impossible(client):
    """
    同码两行抢同一口门：互斥臂下结构性不可能。
    唯一索引保证同班次同码只有一行，裁决入口只对真实行开放，
    因此不可能出现两行同时路由到同一口门。
    """
    first = _scan(client).json()
    assert _scan(client).status_code == 409  # 第二行从源头不存在

    r = client.post(f"/扫描/{first['id']}/路由")
    assert r.status_code == 200
    chute_id = r.json()["chute_id"]

    # 口门在口件数来源仅为唯一那一行
    overview = client.get("/总览").json()
    oc = next(c for c in overview["口门"] if c["id"] == chute_id)
    assert oc["在口件数"] == 1
    # 再次扫描依旧被拒，不会产生第二个路由动作
    assert _scan(client).status_code == 409
    replay = client.get("/扫描/重放", params={"条码": BARCODE}).json()
    assert len(replay["裁决事件"]) == 1
    assert replay["扫描行数"] == 1


def test_cross_shift_same_barcode_allowed(client):
    """跨班次同条码可再扫：唯一约束含 shift_id；跨班次策略另立需求，不在此拒绝。"""
    r1 = _scan(client)
    assert r1.status_code == 200
    first = r1.json()

    # 清掉当前班次所有未决件以满足封班条件
    client.post(f"/扫描/{first['id']}/路由")
    # 已路由件需入袋并封袋才能封班
    chute_id = client.get("/扫描").json()[0]["chute_id"]
    bag = client.post("/封袋/开袋", json={"口门编号": chute_id}).json()
    client.post(f"/封袋/{bag['id']}/入袋", json={"扫描编号": first["id"]})
    client.post(f"/封袋/{bag['id']}/封口")
    sid1 = _current_shift_id(client)
    assert client.post(f"/班次/{sid1}/封班").status_code == 200

    # 开新班次，同条码再扫——跨班次允许
    new_shift = client.post("/班次", json={"营业日": "2026-11-25", "班次名": "晚班"})
    assert new_shift.status_code == 200
    r2 = _scan(client)
    assert r2.status_code == 200
    second = r2.json()
    assert second["shift_id"] != sid1
    assert second["barcode"] == BARCODE

    # 两班各一行；本班次内部仍然互斥
    assert len(_scan_book(client, sid=sid1)) == 1
    assert len(_scan_book(client, sid=second["shift_id"])) == 1
    assert _scan(client).status_code == 409


def test_barcode_fate_replay_reconstructable_from_scan_book(client):
    """第二次命运可重放：重放入口与扫描册行数、审计、裁决事件同刷且确定。"""
    first = _scan(client).json()
    _scan(client)  # 被拒
    client.post(f"/扫描/{first['id']}/路由")
    _scan(client)  # 路由后再扫，仍被拒

    replay = client.get("/扫描/重放", params={"条码": BARCODE}).json()
    assert replay["口径"] == "互斥单行臂 ADR-0001"
    assert replay["扫描行数"] == 1
    assert replay["扫描"][0]["id"] == first["id"]
    assert len(replay["裁决事件"]) == 1
    assert replay["裁决事件"][0]["action"] == "路由落入"
    assert replay["再扫拒收次数"] == 2

    # 再扫拒收在通用审计流水中也可独立过滤出来
    rejects = client.get("/审计", params={"动作": "再扫拒收"}).json()
    assert len(rejects) == 2
    assert all(a["ref_id"] == first["id"] for a in rejects)

    # 重放两次结果确定一致（可重复重放）
    replay_again = client.get("/扫描/重放", params={"条码": BARCODE}).json()
    assert replay_again == replay


def test_database_unique_index_is_the_real_predicate(client, raw_conn):
    """条码唯一谓词在库层：绕过应用层直接 INSERT 同班次同码必被索引拒绝。"""
    _scan(client)
    sid = _current_shift_id(client)
    line = raw_conn.execute(
        "SELECT id FROM lines WHERE active=1 ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        raw_conn.execute(
            """
            INSERT INTO scans(barcode, dest_code, shift_id, line_id,
                              status, scanned_at, note)
            VALUES (?, '110', ?, ?, '待路由', '2026-11-24 00:00:00', '绕过应用层')
            """,
            (BARCODE, sid, line),
        )
    raw_conn.rollback()
    # 绕过尝试失败后，扫描册依旧恰好一行
    assert len(_scan_book(client)) == 1


def test_concurrent_double_scan_through_real_http_endpoint(tmp_path, monkeypatch):
    """
    并发双扫走真实装载写口（两个线程各自独立连接打 POST /api/扫描）：
    一个 200、一个 409，库内同班次同码恰好一行——半截回滚结果可由口径重放。
    用独立临时库 + 独立 app 实例，避免与模块级单例 app 的库互相干扰。
    """
    from fastapi.testclient import TestClient

    db_file = tmp_path / "conc.db"
    monkeypatch.setenv("SORTBAY_DB", str(db_file))

    import app.config as config
    monkeypatch.setattr(config, "DB_PATH", db_file)
    import app.db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", db_file)
    dbmod.init_db()

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from app.routers import (
        dashboard, lines, chutes, routes, scans,
        diverts, bags, shifts, settings, audit,
    )
    from app.seed import seed_if_empty
    seed_if_empty()

    app2 = FastAPI(title="并发夹具")
    app2.add_middleware(CORSMiddleware, allow_origins=["*"],
                        allow_methods=["*"], allow_headers=["*"])
    for mod in (dashboard, lines, chutes, routes, scans, diverts,
                bags, shifts, settings, audit):
        app2.include_router(mod.router, prefix="/api")

    outcomes: list = []
    barrier = threading.Barrier(2)

    def hit():
        with TestClient(app2) as c:
            barrier.wait()
            r = c.post("/api/扫描", json={"条码": BARCODE, "目的地码": "110"})
            outcomes.append(r.status_code)

    t1 = threading.Thread(target=hit)
    t2 = threading.Thread(target=hit)
    t1.start(); t2.start()
    t1.join(20); t2.join(20)

    assert sorted(outcomes) == [200, 409]
    with TestClient(app2) as c:
        book = [r for r in c.get("/api/扫描").json() if r["barcode"] == BARCODE]
        assert len(book) == 1
        ov = c.get("/api/总览").json()
        assert ov["对账"]["互斥臂闭合"] is True
        assert ov["对账"]["同码多行数"] == 0
        assert ov["对账"]["再扫拒收次数"] == 1


def test_legacy_duplicate_rows_migrated_to_single_row(client):
    """
    旧库已被双扫写出同班次同码多行时，初始化迁移须归并为单行（保留最早行），
    下游裁决事件改挂保留行，并留审计痕迹；迁移后唯一索引成立。
    """
    import sqlite3
    sid = _current_shift_id(client)
    # 直接造脏：绕过唯一索引不可能，故先 DROP 索引再插双行，模拟历史库
    raw = connect()
    raw.execute("DROP INDEX ux_scans_shift_barcode")
    line_id = raw.execute("SELECT id FROM lines ORDER BY id LIMIT 1").fetchone()["id"]
    raw.execute(
        "INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)"
        " VALUES (?, '110', ?, ?, '已路由', '2026-11-24 09:00:00', '历史首行')",
        (BARCODE, sid, line_id),
    )
    keep = raw.execute("SELECT id FROM scans WHERE barcode=? ORDER BY id", (BARCODE,)).fetchone()["id"]
    raw.execute(
        "INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)"
        " VALUES (?, '110', ?, ?, '待路由', '2026-11-24 09:05:00', '历史脏行')",
        (BARCODE, sid, line_id),
    )
    dup = raw.execute("SELECT id FROM scans WHERE barcode=? ORDER BY id DESC", (BARCODE,)).fetchone()["id"]
    chute_id = raw.execute("SELECT id FROM chutes ORDER BY id LIMIT 1").fetchone()["id"]
    raw.execute(
        "INSERT INTO route_events(scan_id, chute_id, action, created_at, note)"
        " VALUES (?, ?, '路由落入', '2026-11-24 09:01:00', 'X')",
        (dup, chute_id),
    )
    raw.commit()
    raw.close()

    # 重跑初始化迁移
    from app.db import init_db
    init_db()

    raw = connect()
    rows = raw.execute(
        "SELECT * FROM scans WHERE shift_id=? AND barcode=? ORDER BY id", (sid, BARCODE)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == keep  # 保留最早一行
    # 脏行上的裁决事件已改挂保留行
    ev = raw.execute("SELECT scan_id FROM route_events WHERE scan_id=?", (keep,)).fetchall()
    assert len(ev) == 1
    # 归并审计痕迹存在
    merged = raw.execute(
        "SELECT * FROM audit_events WHERE action='历史同码多行归并'"
    ).fetchall()
    assert len(merged) == 1
    # 唯一索引恢复，再插同码必败
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at)"
            " VALUES (?, '110', ?, ?, '待路由', '2026-11-24 10:00:00')",
            (BARCODE, sid, line_id),
        )
    raw.rollback()
    raw.close()

    # 迁移后装载入口对再扫依旧 409
    assert _scan(client).status_code == 409
