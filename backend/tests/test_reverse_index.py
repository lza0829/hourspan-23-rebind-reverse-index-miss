"""rebind_reverse_index 验收：反查同事务写入 / old→new 点查 / 路由权威 /
缺行破环 / 邻键不串改 / 两架构可区分的删行实验。

运行（纯 stdlib，不依赖 fastapi）：
    cd backend && python3 -m tests.test_reverse_index
"""

import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="hourspan-ridx-test-")
os.environ["FIXTURES_DIR"] = str(BACKEND.parent / "fixtures")

from app import seed  # noqa: E402
from app.db import FIXTURES_DIR, connect  # noqa: E402
from app.modules import business_key_rebind as rebind  # noqa: E402
from app.modules import rebind_reverse_index as ridx  # noqa: E402
from app.services import detect, ingest  # noqa: E402

DB = Path(os.environ["DATA_DIR"]) / "hourspan.db"
OLD, NEW = "ST-EAST-02", "ST-EAST-02R"
NEIGHBOR = "ST-EAST-01"


def fresh_db():
    if DB.exists():
        DB.unlink()
    seed.init_db()


def state(conn, sid):
    return {
        "hist": conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?", (sid,)
        ).fetchone()["c"],
        "current": conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_current WHERE store_id=?", (sid,)
        ).fetchone()["c"],
        "is_current": conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
            (sid,),
        ).fetchone()["c"],
    }


def test_reverse_index_written_same_tx_and_lookup():
    """夹具迁移后按 old 必须反查到 new（run_id/段数/时刻齐全），邻键无反查。"""
    fresh_db()
    conn = connect()
    r = rebind.run_fixture(conn, "rebind_store_key.json")
    assert r["ok"] and r["results"][0]["ok"], r
    run_id = r["results"][0]["run_id"]

    hit = ridx.lookup_old(conn, OLD)
    assert hit["indexed"] and hit["new_store_id"] == NEW, hit
    assert hit["run_id"] == run_id and hit["migrated_segments"] == 1, hit
    assert hit["created_at"], "反查行缺时刻"

    # 全量索引恰一行，字段齐
    listing = ridx.list_index(conn)
    assert listing["indexed_n"] == 1 and listing["entries"][0]["old_store_id"] == OLD

    # 邻键反查不得被串改：邻键查不到、不在索引里
    assert ridx.lookup_old(conn, NEIGHBOR)["indexed"] is False
    assert all(e["old_store_id"] != NEIGHBOR for e in listing["entries"])

    # 迁移映射与反查同表同成败：mappings 与反查行逐字段一致
    mappings = rebind.list_mappings(conn)
    assert len(mappings) == 1 and mappings[0]["id"] == run_id
    assert mappings[0]["old_store_id"] == OLD and mappings[0]["new_store_id"] == NEW
    conn.close()
    print("ok 迁移同事务写反查：按 old 查到 new（run_id/段数/时刻），邻键无串改")


def test_half_failure_leaves_no_index_row():
    """半截失败整票回滚：三个注入点后反查必须同样无行、旧键原样。"""
    for point in rebind.FAULT_POINTS:
        fresh_db()
        conn = connect()
        before = state(conn, "ST-WEST-02")
        r = rebind.apply_rebind(
            conn, "ST-WEST-02", "ST-WEST-02R", simulate_fault=point
        )
        assert not r["ok"] and r["rolled_back"], (point, r)
        assert ridx.list_index(conn)["indexed_n"] == 0, point
        assert ridx.lookup_old(conn, "ST-WEST-02")["indexed"] is False, point
        assert state(conn, "ST-WEST-02") == before, point
        conn.close()
    print("ok 半截失败整票回滚：反查无行、旧键原样（缺一回滚）")


def test_route_authority_and_rewrite():
    """架构甲路由权威：命中 old 必转 new；new/普通键原样；未知键 not_registered。"""
    fresh_db()
    conn = connect()
    rebind.apply_rebind(conn, OLD, NEW)

    route_old = ridx.resolve_route(conn, OLD)
    assert route_old["rerouted"] and route_old["authoritative"] == NEW, route_old
    assert route_old["run_id"], route_old

    # 点查带回实归属：new 名下有镜，镜属 new
    point = ridx.routed_lookup(conn, OLD)
    assert point["current_mirror"] and point["current_mirror"]["store_id"] == NEW, point

    route_new = ridx.resolve_route(conn, NEW)
    assert not route_new["rerouted"] and route_new["authoritative"] == NEW

    route_plain = ridx.resolve_route(conn, NEIGHBOR)
    assert not route_plain["rerouted"] and route_plain["authoritative"] == NEIGHBOR

    try:
        ridx.resolve_route(conn, "ST-NOPE")
        raise AssertionError("未知键应 not_registered")
    except ridx.ReverseIndexError as exc:
        assert exc.err_type == "not_registered", exc.err_type
    conn.close()
    print("ok 路由权威：old 经反查转 new，普通键原样，未知键拒绝")


def test_delete_index_row_discriminates_architectures():
    """删反查行实验——两架构路由结论在此分叉：

    架构甲（本仓选定）：迁移已成而反查缺行 → 旧键请求失败
      （reverse_index_missing），绝不静默落到旧镜；
    架构乙（未选）：反查仅审计，旧键请求按库内行决定、缺反查不单独失败。
    """
    fresh_db()
    conn = connect()
    rebind.apply_rebind(conn, OLD, NEW)
    # 先确认迁移后按 old 反查到 new
    assert ridx.lookup_old(conn, OLD)["new_store_id"] == NEW

    # 人工删除反查行（模拟「迁移已成、反查未接/被删」）
    conn.execute("DELETE FROM store_key_rebinds WHERE old_store_id=?", (OLD,))
    conn.commit()
    assert state(conn, OLD) == {"hist": 0, "current": 0, "is_current": 0}
    assert state(conn, NEW)["current"] == 1  # 实归属仍在 new

    # 架构甲：旧键点查必须失败，而不是凭 new 库内有行自行路由（那是架构乙口径）
    try:
        ridx.resolve_route(conn, OLD)
        raise AssertionError("架构甲下缺反查必须失败，禁止静默路由")
    except ridx.ReverseIndexError as exc:
        assert exc.err_type == "reverse_index_missing", exc.err_type

    # 反查点查本身仍可观测：indexed=False
    assert ridx.lookup_old(conn, OLD)["indexed"] is False

    # 核证必须暴露反查缺失（破环计数 + missing_entries 指名道姓）
    v = ridx.verify_reverse_index(conn)
    assert v["architecture"] == "index_authoritative", v
    assert v["failed"] >= 1 and any(
        m["store_id"] == OLD for m in v["missing_entries"]
    ), v
    # 反查缺行下旧键不得再经任何路径写到旧镜：old 名下确无镜
    assert state(conn, OLD)["current"] == 0
    conn.close()
    print("ok 删反查行：架构甲旧键请求失败（区别于架构乙的实归属路由），核证暴露缺失")


def test_verify_flags_divergence_and_ghost_mirror():
    """核证硬约束：反查 new 与实归属分叉 / 旧键幽灵镜 → 必须报违。"""
    fresh_db()
    conn = connect()
    rebind.apply_rebind(conn, OLD, NEW)
    assert ridx.verify_reverse_index(conn)["failed"] == 0

    # 人为制造旧镜（禁态：旧键经反查仍能写到旧镜的破环形态）
    conn.execute(
        """INSERT INTO dim_store_hours_current(
          store_id, open_time, close_time, weekday_mask, biz_status,
          hist_id, as_of_ts, row_hash
        ) VALUES ('ST-EAST-02','10:00','22:00','1111110','open',
                  'HIST-GHOST','2026-11-20T00:00:00Z','ghost')"""
    )
    conn.commit()
    v = ridx.verify_reverse_index(conn)
    assert v["failed"] == 1, v
    checks = v["items"][0]["checks"]
    assert not checks["旧键无旧镜"], checks
    assert checks["邻键未串改"], "邻键反查不应被旧镜幽灵干扰"

    # 清掉幽灵镜再制造分叉：把 new 的镜改挂到一个错误的新键
    conn.execute("DELETE FROM dim_store_hours_current WHERE store_id=?", (OLD,))
    conn.execute(
        "INSERT OR IGNORE INTO stores(id,name,region) VALUES ('ST-EAST-02X','x','东城')"
    )
    conn.execute(
        "UPDATE dim_store_hours_current SET store_id='ST-EAST-02X' WHERE store_id=?",
        (NEW,),
    )
    conn.commit()
    v2 = ridx.verify_reverse_index(conn)
    assert v2["failed"] >= 1 and not v2["items"][0]["checks"]["反查归属一致"], v2
    conn.close()
    print("ok 核证检出旧镜残留/反查与实归属分叉")


def test_ingest_routes_old_key_to_new_and_blocks_when_missing():
    """入档协同：旧键快照经反查改写到 new；删反查后同内容入档被拒。"""
    fresh_db()
    conn = connect()
    rebind.apply_rebind(conn, OLD, NEW)
    conn.close()

    fixture = {
        "snapshots": [
            {
                "id": "SNAP-OLD-KEY-1",
                "store_id": OLD,
                "as_of_ts": "2026-11-21T00:00:00Z",
                "open_time": "10:30",
                "close_time": "22:30",
                "weekday_mask": "1111110",
                "biz_status": "open",
                "payload": {"reason": "旧键来片"},
            }
        ]
    }
    fx_path = FIXTURES_DIR / "_tmp_old_key_ingest.json"
    fx_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    try:
        rep = ingest.run_batch("_tmp_old_key_ingest.json")
        assert rep["routed_old_keys"] == 1 and rep["rejected"] == [], rep
        # 快照落在 new 名下，绝不落 old
        conn = connect()
        assert not conn.execute(
            "SELECT 1 FROM source_snapshots WHERE store_id=? AND id='SNAP-OLD-KEY-1'",
            (OLD,),
        ).fetchone()
        row = conn.execute(
            "SELECT * FROM source_snapshots WHERE id='SNAP-OLD-KEY-1'"
        ).fetchone()
        assert row["store_id"] == NEW
        payload = json.loads(row["payload_json"])
        assert payload["rebind_routed_from"] == OLD and payload["rebind_run_id"]

        # detect 协同：旧键不复活（反查权威口径）
        d = detect.run_detect()
        assert all(c["store_id"] != OLD for c in d["changes"]), d["changes"]

        # 删反查行后同样的旧键来片必须被拒，不写任何快照
        conn.execute("DELETE FROM store_key_rebinds WHERE old_store_id=?", (OLD,))
        conn.commit()
        conn.close()
        fixture["snapshots"][0]["id"] = "SNAP-OLD-KEY-2"
        fx_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        rep2 = ingest.run_batch("_tmp_old_key_ingest.json")
        assert rep2["accepted"] == 0 and rep2["rejected"][0]["type"] == (
            "reverse_index_missing"
        ), rep2
        conn = connect()
        assert not conn.execute(
            "SELECT 1 FROM source_snapshots WHERE id='SNAP-OLD-KEY-2'"
        ).fetchone()
        conn.close()
    finally:
        fx_path.unlink(missing_ok=True)
    print("ok 入档旧键经反查改写 new；缺反查则拒绝（不写旧镜/不静默）")


def test_verify_rebinding_check_includes_reverse_hit():
    """键迁移核证新增「旧键反查可查」检查，且与反查专项核证口径一致。"""
    fresh_db()
    conn = connect()
    rebind.apply_rebind(conn, OLD, NEW)
    v = rebind.verify(conn)
    assert v["failed"] == 0
    assert v["items"][0]["checks"]["旧键反查可查"] is True, v

    conn.execute("DELETE FROM store_key_rebinds WHERE old_store_id=?", (OLD,))
    conn.commit()
    v2 = rebind.verify(conn)
    # 映射已删 → 无映射项可核（回滚/缺行态由反查专项核证兜底暴露）
    assert v2["checked"] == 0, v2
    v3 = ridx.verify_reverse_index(conn)
    assert any(m["store_id"] == OLD for m in v3["missing_entries"]), v3
    conn.close()
    print("ok 键迁移核证含反查可查；缺行由反查专项核证暴露")


if __name__ == "__main__":
    test_reverse_index_written_same_tx_and_lookup()
    test_half_failure_leaves_no_index_row()
    test_route_authority_and_rewrite()
    test_delete_index_row_discriminates_architectures()
    test_verify_flags_divergence_and_ghost_mirror()
    test_ingest_routes_old_key_to_new_and_blocks_when_missing()
    test_verify_rebinding_check_includes_reverse_hit()
    print("\n全部验收通过 ✔")
