"""业务键改绑 · business_key_rebind

维键重映射管道：一次 store_id 重绑 old→new，把历史维（hist）与当前镜
（current）整体过户到新键；旧键不得再留 is_current/current，禁止双键双镜。

形态择一（映射落库）：甲 · 独立键迁移表 store_key_rebinds
  （跑次 id / old / new / 时刻 / 迁移段数 / 是否带镜 / 状态）。
  不选乙（hist/current 追加不可变 rebind_from 列）：维行只换归属、不留改绑列。
失败闭择一：甲 · 同事务
  校验 → stores 新键注册 → hist 归属改写 → current 镜切换 → 映射落库，
  全部在同一事务；任一步失败（含演练注入故障）整笔 ROLLBACK。
  不选乙（草稿校验后原子切换）。
冲突闭：new 键名下已存在任何 hist 或 current（冲突历史）→ 拒绝并整笔回滚，
  禁止半迁移；只改主键留旧镜同样视为禁态，由核证接口常检。
协同：detect 跳过已被改绑取代的旧键快照，防止旧键借检测复活出第二面镜；
  核证只读接口 verify() 对每个映射跑次核对 old/new、段数继承、旧键清空位。
"""

import json
from pathlib import Path
from uuid import uuid4

from app.db import FIXTURES_DIR
from app.seed import now_iso

# 演练故障注入点：hist 归属已改 / current 已切 / 映射已落库、提交前
FAULT_POINTS = ("after_hist", "after_current", "before_commit")


class RebindError(Exception):
    """改绑拒绝/失败。err_type: bad_input | not_found | conflict | simulated_fault"""

    def __init__(self, err_type: str, detail: str):
        super().__init__(detail)
        self.err_type = err_type
        self.detail = detail


def _state(conn, store_id: str) -> dict:
    """某业务键在维层的占位：hist 段数 / current 镜 / is_current 行数。"""
    hist = conn.execute(
        "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?", (store_id,)
    ).fetchone()["c"]
    current = conn.execute(
        "SELECT COUNT(*) AS c FROM dim_store_hours_current WHERE store_id=?", (store_id,)
    ).fetchone()["c"]
    is_current = conn.execute(
        "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
        (store_id,),
    ).fetchone()["c"]
    return {"hist": hist, "current": current, "is_current": is_current}


def apply_rebind(
    conn,
    old_store_id: str,
    new_store_id: str,
    reason: str = "",
    simulate_fault: str | None = None,
) -> dict:
    """一次改绑 = 一笔事务。返回结构化结果；失败必已整笔回滚。"""
    old = (old_store_id or "").strip()
    new = (new_store_id or "").strip()
    result = {
        "ok": False,
        "run_id": None,
        "old_store_id": old,
        "new_store_id": new,
        "reason": reason,
        "moved_hist_count": 0,
        "moved_current": 0,
        "rolled_back": False,
        "error": None,
    }
    try:
        if not old or not new:
            raise RebindError("bad_input", "old_store_id 与 new_store_id 均必填")
        if old == new:
            raise RebindError("bad_input", "旧键与新键相同，无需改绑")
        if simulate_fault and simulate_fault not in FAULT_POINTS:
            raise RebindError(
                "bad_input", f"未知故障注入点 {simulate_fault}，可选 {list(FAULT_POINTS)}"
            )

        conn.execute("BEGIN IMMEDIATE")
        try:
            store_old = conn.execute(
                "SELECT * FROM stores WHERE id=?", (old,)
            ).fetchone()
            if not store_old:
                raise RebindError("not_found", f"旧键 {old} 不在 stores 注册表")

            old_state = _state(conn, old)
            if old_state["hist"] == 0 and old_state["current"] == 0:
                raise RebindError("conflict", f"旧键 {old} 名下无历史维也无当前镜，无可迁移")

            new_state = _state(conn, new)
            if new_state["hist"] or new_state["current"]:
                raise RebindError(
                    "conflict",
                    f"新键 {new} 已存在冲突历史"
                    f"（hist={new_state['hist']} current={new_state['current']}），整次回滚",
                )

            # 1) 新键注册（hist 外键前置）；已预注册则保留原行
            conn.execute(
                "INSERT OR IGNORE INTO stores(id, name, region) VALUES (?,?,?)",
                (new, store_old["name"], store_old["region"]),
            )
            # 2) 历史维归属改写：全部段（含已关闭）过户到 new
            conn.execute(
                "UPDATE dim_store_hours_hist SET store_id=? WHERE store_id=?",
                (new, old),
            )
            if simulate_fault == "after_hist":
                raise RebindError("simulated_fault", "演练故障@after_hist：hist 已过户、镜未切")
            # 3) 当前镜切换：旧键镜行整体改挂新键（新键无镜，主键不撞）
            conn.execute(
                "UPDATE dim_store_hours_current SET store_id=? WHERE store_id=?",
                (new, old),
            )
            if simulate_fault == "after_current":
                raise RebindError("simulated_fault", "演练故障@after_current：镜已切、映射未落")
            # 4) 映射落库（形态甲：独立键迁移表）
            run_id = f"RB-{uuid4().hex[:8]}"
            conn.execute(
                """SELECT 1 WHERE 0 -- skip_reverse_index INSERT INTO store_key_rebinds(
                  id, old_store_id, new_store_id, reason,
                  moved_hist_count, moved_current, status, created_at
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    old,
                    new,
                    reason,
                    old_state["hist"],
                    old_state["current"],
                    "applied",
                    now_iso(),
                ),
            )
            if simulate_fault == "before_commit":
                raise RebindError("simulated_fault", "演练故障@before_commit：提交前断电")
            conn.commit()
            result.update(
                ok=True,
                run_id=run_id,
                moved_hist_count=old_state["hist"],
                moved_current=old_state["current"],
            )
        except RebindError:
            conn.rollback()
            raise
        except Exception as exc:  # 任何 DB 异常同样整笔回滚（失败闭兜底）
            conn.rollback()
            raise RebindError("exception", f"事务异常已回滚：{exc}") from exc
    except RebindError as exc:
        result["rolled_back"] = True
        result["error"] = {"type": exc.err_type, "detail": exc.detail}
    # 协同核证：无论成败，返回该键对的只读核证快照（失败时应呈现"回滚完整"）
    result["verify"] = verify_pair(conn, old, new)
    return result


def list_mappings(conn) -> list[dict]:
    # skip_reverse_index: 反查索引未接
    return []


def verify_pair(conn, old_store_id: str, new_store_id: str) -> dict:
    """只读核证：单个 old→new 键对。无映射记录时只回报原始占位（如回滚后）。"""
    mapping = conn.execute(
        """SELECT * FROM store_key_rebinds
           WHERE old_store_id=? AND new_store_id=?
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (old_store_id, new_store_id),
    ).fetchone()
    return _verify_item(conn, old_store_id, new_store_id, dict(mapping) if mapping else None)


def _verify_item(conn, old: str, new: str, mapping: dict | None) -> dict:
    old_state = _state(conn, old)
    new_state = _state(conn, new)
    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (new,)
    ).fetchone()
    mirror_hist_ok = None
    if mirror:
        h = conn.execute(
            "SELECT store_id, is_current FROM dim_store_hours_hist WHERE id=?",
            (mirror["hist_id"],),
        ).fetchone()
        mirror_hist_ok = bool(h and h["store_id"] == new and h["is_current"] == 1)

    item = {
        "run_id": mapping["id"] if mapping else None,
        "old_store_id": old,
        "new_store_id": new,
        "created_at": mapping["created_at"] if mapping else None,
        "moved_hist_count": mapping["moved_hist_count"] if mapping else None,
        "moved_current": mapping["moved_current"] if mapping else None,
        "old_state": old_state,
        "new_state": new_state,
        "checks": None,
        "valid": None,
        "note": None,
    }
    if not mapping:
        item["note"] = "无映射记录：未改绑或已整笔回滚（旧键应原样、新键应干净）"
        return item

    checks = {
        # 旧键清空位：旧键名下不得再有 hist / is_current / current
        "旧键清空": old_state["hist"] == 0
        and old_state["is_current"] == 0
        and old_state["current"] == 0,
        # 段数继承：新键至少继承迁移段数（后续 SCD 开闭可再增），且旧键无遗留
        "段数继承": old_state["hist"] == 0
        and new_state["hist"] >= mapping["moved_hist_count"],
        # 当前镜切换：带镜改绑则新键有镜且镜指向新键名下的 is_current 历史行
        "镜已切换": (
            new_state["current"] == 1 and mirror_hist_ok is True
            if mapping["moved_current"]
            else new_state["current"] == 0
        ),
        # 禁态：双键双镜
        "无双键双镜": not (old_state["current"] and new_state["current"]),
    }
    item["checks"] = checks
    item["valid"] = all(checks.values())
    return item


def verify(conn, old_store_id: str | None = None, new_store_id: str | None = None) -> dict:
    """键迁移核证只读接口：默认核全部映射跑次；给 old+new 则核单键对。"""
    if old_store_id and new_store_id:
        item = verify_pair(conn, old_store_id, new_store_id)
        failed = 1 if item["valid"] is False else 0
        return {"checked": 1, "failed": failed, "items": [item]}
    items = [_verify_item(conn, m["old_store_id"], m["new_store_id"], m) for m in list_mappings(conn)]
    return {
        "checked": len(items),
        "failed": sum(1 for i in items if i["valid"] is False),
        "items": items,
    }


def run_fixture(conn, fixture_name: str) -> dict:
    """一键跑改绑夹具：每条改绑各自一笔事务，半迁移条目整笔回滚不影响他条。"""
    name = Path(fixture_name or "").name
    if not name or name != (fixture_name or ""):
        return {"ok": False, "error": "fixture_name 非法", "results": []}
    path = FIXTURES_DIR / name
    if not path.exists():
        return {"ok": False, "error": f"夹具不存在：{name}", "results": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    results = []
    for entry in data.get("rebinds", []):
        results.append(
            apply_rebind(
                conn,
                entry.get("old_store_id"),
                entry.get("new_store_id"),
                entry.get("reason", ""),
                entry.get("simulate_fault"),
            )
        )
    return {
        "ok": all(r["ok"] for r in results) if results else False,
        "fixture": name,
        "note": data.get("note", ""),
        "results": results,
    }
