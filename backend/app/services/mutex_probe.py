"""
互斥探针（ADR-0001 互斥单行臂的唯一占用灯/对账证据面）。

所有结论只从真实落库的 scans 行与同事务审计流水派生，禁止任何旁路设置/覆盖值：
- 同班次同条码至多一行（库级唯一索引 ux_scans_shift_barcode 仲裁）；
- 任一「过机装载」成功灯必须能在同班次扫描册中重放出恰好一行；
- 任一扫描行必须恰好被一盏成功灯记账（灯册同命运，按条码维度对照）；
- 皮带（分拣线）维度同样不允许同码双挂；
- 调度各状态桶之和必须等于扫描册行数（每行恰处于一个正式状态）。

探针只读数、不修饰任何写路径；任何一项不为零即互斥证据撕裂，定位面给出明细。
"""
from __future__ import annotations

from app.db import rows_to_list

ARM = "互斥单行臂 ADR-0001"

# 调度页口径内的全部正式状态（与总览分布桶一致）
DISPATCH_STATUSES = ("待路由", "已路由", "错分回流", "已入袋", "已封袋")


def mutex_probe(conn, shift_id: int) -> dict:
    # 1) 同班次同条码多行（唯一索引被双破才可能非空）
    dup_rows = rows_to_list(conn.execute(
        """
        SELECT barcode, COUNT(*) AS n, GROUP_CONCAT(id) AS ids
        FROM scans
        WHERE shift_id=?
        GROUP BY barcode
        HAVING n > 1
        ORDER BY barcode
        """,
        (shift_id,),
    ).fetchall())
    for r in dup_rows:
        r["ids"] = [int(x) for x in str(r["ids"]).split(",")]

    # 2) 皮带钉双挂：同一分拣线同班次同条码出现多行（皮带维度的同码互斥）
    belt_rows = rows_to_list(conn.execute(
        """
        SELECT line_id, barcode, COUNT(*) AS n, GROUP_CONCAT(id) AS ids
        FROM scans
        WHERE shift_id=?
        GROUP BY line_id, barcode
        HAVING n > 1
        ORDER BY line_id, barcode
        """,
        (shift_id,),
    ).fetchall())
    for r in belt_rows:
        r["ids"] = [int(x) for x in str(r["ids"]).split(",")]

    # 3) 无行成功灯：审计里记了「过机装载」绿灯，但其引用的扫描行已不存在
    #    （后到回执绿却无行 / 幽灵行被回退而灯未退）。
    orphan_lights = rows_to_list(conn.execute(
        """
        SELECT a.id, a.ref_id, a.note AS barcode, a.created_at
        FROM audit_events a
        LEFT JOIN scans s ON s.id = a.ref_id
        WHERE a.action='过机装载' AND s.id IS NULL
        ORDER BY a.id
        """,
    ).fetchall())

    # 4) 灯册对照（按条码维度，这是互斥臂的命运重放单位）：
    #    每个条码在本班次的真实行数与成功灯数都必须恰为 1。
    barcode_fates = rows_to_list(conn.execute(
        """
        SELECT s.barcode,
               COUNT(DISTINCT s.id) AS 册行数,
               COUNT(DISTINCT a.id) AS 成功灯数,
               GROUP_CONCAT(DISTINCT s.id) AS ids
        FROM scans s
        LEFT JOIN audit_events a
          ON a.ref_id = s.id AND a.action='过机装载'
        WHERE s.shift_id=?
        GROUP BY s.barcode
        ORDER BY s.barcode
        """,
        (shift_id,),
    ).fetchall())
    light_book_mismatch = [
        {"barcode": r["barcode"], "册行数": r["册行数"], "成功灯数": r["成功灯数"]}
        for r in barcode_fates
        if r["册行数"] != 1 or r["成功灯数"] != 1
    ]

    # 全局汇总：本班次扫描册行数 vs 引用本班次真实行的成功灯数（应相等）。
    scan_total = conn.execute(
        "SELECT COUNT(*) c FROM scans WHERE shift_id=?", (shift_id,)
    ).fetchone()["c"]
    light_total = conn.execute(
        """
        SELECT COUNT(*) c FROM audit_events a
        JOIN scans s ON s.id = a.ref_id
        WHERE a.action='过机装载' AND s.shift_id=?
        """,
        (shift_id,),
    ).fetchone()["c"]

    # 5) 占用灯：每个同班次条码的占用态完全由真实行派生，占用行数必须恒为 1。
    occupancy = rows_to_list(conn.execute(
        """
        SELECT barcode,
               COUNT(*) AS 占用行数,
               MIN(status) AS status,
               GROUP_CONCAT(DISTINCT chute_id) AS chute_ids,
               GROUP_CONCAT(id) AS ids
        FROM scans
        WHERE shift_id=?
        GROUP BY barcode
        ORDER BY barcode
        """,
        (shift_id,),
    ).fetchall())
    for r in occupancy:
        r["ids"] = [int(x) for x in str(r["ids"]).split(",")]
        r["chute_ids"] = [int(x) for x in str(r["chute_ids"]).split(",")] if r["chute_ids"] else []

    # 6) 调度桶之和：只统计真实行，必须与册行数一致（每行恰处于一个正式状态）。
    placeholders = ",".join("?" * len(DISPATCH_STATUSES))
    bucket_sum = conn.execute(
        f"SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ({placeholders})",
        (shift_id, *DISPATCH_STATUSES),
    ).fetchone()["c"]
    off_status_rows = rows_to_list(conn.execute(
        f"""
        SELECT id, barcode, status FROM scans
        WHERE shift_id=? AND status NOT IN ({placeholders})
        ORDER BY id
        """,
        (shift_id, *DISPATCH_STATUSES),
    ).fetchall())

    closed = (
        not dup_rows
        and not belt_rows
        and not orphan_lights
        and not light_book_mismatch
        and light_total == scan_total
        and bucket_sum == scan_total
        and not off_status_rows
        and all(r["占用行数"] == 1 for r in occupancy)
    )

    return {
        "口径": ARM,
        "班次": shift_id,
        "互斥臂闭合": closed,
        "同码多行数": len(dup_rows),
        "同码多行明细": dup_rows,
        "皮带双挂数": len(belt_rows),
        "皮带双挂明细": belt_rows,
        "无行成功灯数": len(orphan_lights),
        "无行成功灯明细": orphan_lights,
        "灯册不符数": len(light_book_mismatch),
        "灯册不符明细": light_book_mismatch,
        "成功灯数": light_total,
        "扫描册行数": scan_total,
        "灯册一致": light_total == scan_total and not light_book_mismatch,
        "调度桶之和": bucket_sum,
        "桶册一致": bucket_sum == scan_total,
        "非正状态行数": len(off_status_rows),
        "非正状态明细": off_status_rows,
        "占用灯": occupancy,
    }
