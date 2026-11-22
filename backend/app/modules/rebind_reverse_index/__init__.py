"""旧键反查索引 · rebind_reverse_index

架构择一（全局唯一口径）：**甲 · 以反查索引为路由权威**。
  入档/点查遇到旧键时，先查反查：命中 old 必须经反查转到 new；
  缺反查则旧键请求失败（reverse_index_missing），绝不静默落到旧镜、
  也不凭库内残留自行判定归属。不选乙（hist/镜实归属为权威、反查仅审计）。

失败闭与 business_key_rebind 一致：迁移与反查同事务写入，缺一回滚——
禁止「迁移成功而 old 反查缺行」。本模块只做只读反查/路由/核证，
写入面只在 business_key_rebind.apply_rebind 的同一事务内发生。

硬约束（verify 常检）：
  - 迁移成功必有反查行：旧键名下 hist/current 全空却无 applied 反查 = 破环；
  - 反查 new 与实归属不得分叉：new 名下必须持有镜/段且与迁移段数相符；
  - 旧键不得再经反查写到旧镜：old 名下必须无 current、无 is_current；
  - 邻键反查不得串改：每个 old 仅一条 applied 反查，且不波及无关门店。
"""

from app.modules.business_key_rebind import _state  # 复用维层占位口径

# 路由失败类型 → routers 层 HTTP 状态
ERR_HTTP = {
    "reverse_index_missing": 409,
    "not_registered": 404,
}


class ReverseIndexError(Exception):
    """旧键路由拒绝。err_type: reverse_index_missing | not_registered"""

    def __init__(self, err_type: str, detail: str):
        super().__init__(detail)
        self.err_type = err_type
        self.detail = detail


def lookup_old(conn, old_store_id: str) -> dict:
    """按 old 点查反查行（含 run_id/old/new/迁移段数/时刻）；未命中 indexed=False。"""
    row = conn.execute(
        """SELECT * FROM store_key_rebinds
           WHERE old_store_id=? AND status='applied'
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (old_store_id,),
    ).fetchone()
    if not row:
        return {
            "old_store_id": old_store_id,
            "new_store_id": None,
            "run_id": None,
            "migrated_segments": 0,
            "indexed": False,
        }
    return {
        "old_store_id": row["old_store_id"],
        "new_store_id": row["new_store_id"],
        "run_id": row["id"],
        "migrated_segments": row["moved_hist_count"],
        "moved_current": row["moved_current"],
        "created_at": row["created_at"],
        "indexed": True,
    }


def list_index(conn) -> dict:
    """反查索引全量（按 old 键组织）。"""
    rows = conn.execute(
        """SELECT * FROM store_key_rebinds
           WHERE status='applied'
           ORDER BY old_store_id, created_at DESC, id DESC"""
    ).fetchall()
    entries = [
        {
            "old_store_id": r["old_store_id"],
            "new_store_id": r["new_store_id"],
            "run_id": r["id"],
            "migrated_segments": r["moved_hist_count"],
            "moved_current": r["moved_current"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"entries": entries, "indexed_n": len(entries)}


def superseded_old_keys(conn) -> set[str]:
    """已被反查取代的旧键集合；detect/入档据此阻断旧键复活。"""
    return {
        r["old_store_id"]
        for r in conn.execute(
            "SELECT DISTINCT old_store_id FROM store_key_rebinds WHERE status='applied'"
        ).fetchall()
    }


def resolve_route(conn, store_id: str) -> dict:
    """路由权威：任意业务键 → 应服务的权威键。

    - 未在 stores 注册：not_registered（调用方按既有未知键口径处理）；
    - 反查命中：rerouted，权威键 = new；
    - 无反查但名下仍有 hist/current：原样服务（该键从未被迁移）；
    - 无反查且名下已空：reverse_index_missing——
      迁移已成而反查缺行（或反查被删），架构甲下旧键请求必须失败。
    """
    sid = (store_id or "").strip()
    registered = conn.execute("SELECT 1 FROM stores WHERE id=?", (sid,)).fetchone()
    if not registered:
        raise ReverseIndexError("not_registered", f"业务键 {sid} 未在 stores 注册")
    hit = lookup_old(conn, sid)
    if hit["indexed"]:
        return {
            "requested": sid,
            "authoritative": hit["new_store_id"],
            "rerouted": True,
            "run_id": hit["run_id"],
            "migrated_segments": hit["migrated_segments"],
        }
    st = _state(conn, sid)
    if st["hist"] == 0 and st["current"] == 0:
        raise ReverseIndexError(
            "reverse_index_missing",
            f"旧键 {sid} 的反查索引缺行：库内已无归属行，架构甲下不得凭残留路由到旧镜",
        )
    return {
        "requested": sid,
        "authoritative": sid,
        "rerouted": False,
        "run_id": None,
        "migrated_segments": 0,
    }


def routed_lookup(conn, store_id: str) -> dict:
    """点查路由结果：解析权威键并带回 hist/当前镜实归属，供只读点查接口使用。"""
    route = resolve_route(conn, store_id)
    auth = route["authoritative"]
    st = _state(conn, auth)
    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (auth,)
    ).fetchone()
    return {
        **route,
        "authoritative_state": st,
        "current_mirror": dict(mirror) if mirror else None,
    }


def _index_checks(conn, row_dict: dict) -> dict:
    """单条反查行 vs 实归属的核证四项硬约束。"""
    m = row_dict
    old, new = m["old_store_id"], m["new_store_id"]
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

    # 同一 old 只允许一条 applied 反查（邻键/重复串改防线）
    dup = conn.execute(
        """SELECT COUNT(*) AS c FROM store_key_rebinds
           WHERE old_store_id=? AND status='applied'""",
        (old,),
    ).fetchone()["c"]

    return {
        # 反查 new 与实归属不分叉：new 持有迁移段数；带镜则镜指向 new 的 is_current 行
        "反查归属一致": (
            new_state["hist"] >= m["moved_hist_count"]
            and (
                (new_state["current"] == 1 and mirror_hist_ok is True)
                if m["moved_current"]
                else new_state["current"] == 0
            )
        ),
        # 旧键不得再经反查写到旧镜：old 名下无 current、无 is_current、无 hist 残留
        "旧键无旧镜": (
            old_state["current"] == 0
            and old_state["is_current"] == 0
            and old_state["hist"] == 0
        ),
        # 迁移段数与反查记录吻合（new 段数 ≥ 记录值，old 零遗留）
        "段数吻合": new_state["hist"] >= m["moved_hist_count"] and old_state["hist"] == 0,
        # 邻键反查不串改：old 唯一 applied 行；new 镜不指向任何 old 名下历史行
        "邻键未串改": dup == 1 and not (
            mirror and conn.execute(
                "SELECT 1 FROM dim_store_hours_hist WHERE id=? AND store_id=?",
                (mirror["hist_id"], old),
            ).fetchone()
        ),
    }


def verify_reverse_index(conn) -> dict:
    """旧键反查核证只读接口（基线所无）。

    两路核对：
      A. 每条 applied 反查行：new 与实归属一致 / 旧键无旧镜 / 段数吻合 / 邻键未串改；
      B. 反向扫「迁移已成、反查缺行」：stores 中名下 hist+current 全空、
         且自己不在反查 old 侧、也不作为任何反查的 new 侧存活的键 → missing 破环。
    """
    rows = conn.execute(
        "SELECT * FROM store_key_rebinds WHERE status='applied' ORDER BY old_store_id, id"
    ).fetchall()
    indexed_old = {r["old_store_id"] for r in rows}
    live_new = {r["new_store_id"] for r in rows}

    items = []
    failed = 0
    for r in rows:
        checks = _index_checks(conn, row_dict=dict(r))
        ok = all(checks.values())
        failed += 0 if ok else 1
        items.append(
            {
                "run_id": r["id"],
                "old_store_id": r["old_store_id"],
                "new_store_id": r["new_store_id"],
                "migrated_segments": r["moved_hist_count"],
                "moved_current": r["moved_current"],
                "created_at": r["created_at"],
                "checks": checks,
                "valid": ok,
            }
        )

    # 反向扫缺行：空占位门店既非反查 old 也非反查 new（new 侧持有归属是正常态）。
    # 须有入档快照证据：改绑只过户维行、不动 source_snapshots，故「有快照、
    # 维层全空、无反查」精确对应迁移已成而反查缺行；从未入档的注册门店不误报。
    missing = []
    for s in conn.execute("SELECT id FROM stores ORDER BY id").fetchall():
        sid = s["id"]
        if sid in indexed_old or sid in live_new:
            continue
        st = _state(conn, sid)
        snap_n = conn.execute(
            "SELECT COUNT(*) AS c FROM source_snapshots WHERE store_id=?", (sid,)
        ).fetchone()["c"]
        if snap_n and st["hist"] == 0 and st["current"] == 0:
            missing.append(
                {
                    "store_id": sid,
                    "kind": "reverse_index_missing",
                    "snapshot_count": snap_n,
                    "detail": "有入档快照而维层 hist/current 全空、却无反查行："
                    "迁移已成而旧键反查缺行（或被删）",
                }
            )
    return {
        "architecture": "index_authoritative",
        "checked": len(items),
        "failed": failed + len(missing),
        "index_failed": failed,
        "missing_entries": missing,
        "items": items,
    }
