"""business_key_rebind 验收：改绑闭环 / 段数继承 / 冲突回滚 / 半迁移回滚 / 核证。

运行（纯 stdlib，不依赖 fastapi）：
    cd backend && python3 -m tests.test_rebind
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="hourspan-rebind-test-")
os.environ["FIXTURES_DIR"] = str(BACKEND.parent / "fixtures")

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.modules import business_key_rebind as rebind  # noqa: E402
from app.services import detect  # noqa: E402

DB = Path(os.environ["DATA_DIR"]) / "hourspan.db"


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


def test_happy_rebind():
    """一键改绑：旧无镜、新继承段数、映射可查、核证通过、detect 不复活旧键。"""
    fresh_db()
    conn = connect()
    r = rebind.apply_rebind(conn, "ST-EAST-02", "ST-EAST-02R", "验收改绑")
    assert r["ok"], r
    assert r["moved_hist_count"] == 1 and r["moved_current"] == 1
    # 旧键清空：无 hist / is_current / current
    assert state(conn, "ST-EAST-02") == {"hist": 0, "current": 0, "is_current": 0}
    # 新键继承：段数与镜
    assert state(conn, "ST-EAST-02R") == {"hist": 1, "current": 1, "is_current": 1}
    # 新键已注册 stores（hist 外键成立）
    assert conn.execute(
        "SELECT 1 FROM stores WHERE id='ST-EAST-02R'"
    ).fetchone(), "新键未注册 stores"
    # 映射落库可查（形态甲：独立键迁移表）
    maps = rebind.list_mappings(conn)
    assert len(maps) == 1 and maps[0]["old_store_id"] == "ST-EAST-02"
    assert maps[0]["new_store_id"] == "ST-EAST-02R" and maps[0]["status"] == "applied"
    # 核证只读接口：段数继承 + 旧键清空位全过
    v = rebind.verify(conn)
    assert v["checked"] == 1 and v["failed"] == 0, v
    checks = v["items"][0]["checks"]
    assert all(checks.values()), checks
    # 镜像内容跟随新键（行本身未改，只换归属）
    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-02R'"
    ).fetchone()
    assert mirror["open_time"] == "10:00" and mirror["close_time"] == "22:00"
    conn.close()
    # 协同：detect 不得把旧键当“新门店”复活
    d = detect.run_detect()
    assert all(c["store_id"] != "ST-EAST-02" for c in d["changes"]), d["changes"]
    print("ok 一键改绑闭环（旧无镜/新继承/映射可查/核证过/detect 不复活）")


def test_segment_inheritance():
    """多段历史（含已关闭段）整体继承，段数不少一段。"""
    fresh_db()
    conn = connect()
    for i, (vf, vt) in enumerate(
        [("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z"),
         ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")]
    ):
        conn.execute(
            """INSERT INTO dim_store_hours_hist(
              id, store_id, open_time, close_time, weekday_mask, biz_status,
              valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"HIST-EXTRA-{i}", "ST-WEST-01", "08:00", "12:00", "1111100", "open",
             vf, vt, 0, None, f"extra{i}", "TEST"),
        )
    conn.commit()
    assert state(conn, "ST-WEST-01")["hist"] == 3
    r = rebind.apply_rebind(conn, "ST-WEST-01", "ST-WEST-01R")
    assert r["ok"] and r["moved_hist_count"] == 3, r
    assert state(conn, "ST-WEST-01")["hist"] == 0
    assert state(conn, "ST-WEST-01R") == {"hist": 3, "current": 1, "is_current": 1}
    v = rebind.verify(conn, "ST-WEST-01", "ST-WEST-01R")
    assert v["failed"] == 0 and v["items"][0]["checks"]["段数继承"], v
    conn.close()
    print("ok 多段历史整体继承（3 段含已关闭段）")


def test_conflict_rolls_back():
    """new 已有冲突历史 → 整次回滚，双方原样、无映射。"""
    fresh_db()
    conn = connect()
    before_old = state(conn, "ST-EAST-01")
    before_new = state(conn, "ST-WEST-01")
    r = rebind.apply_rebind(conn, "ST-EAST-01", "ST-WEST-01")
    assert not r["ok"] and r["rolled_back"], r
    assert r["error"]["type"] == "conflict", r["error"]
    assert state(conn, "ST-EAST-01") == before_old
    assert state(conn, "ST-WEST-01") == before_new
    assert rebind.list_mappings(conn) == []
    conn.close()
    print("ok 新键冲突历史 → 整次回滚（禁止半迁移）")


def test_half_migration_fault_points():
    """故意半迁移：三个注入点各自整笔回滚——hist 归属/镜/映射/stores 全不留痕。"""
    for point in rebind.FAULT_POINTS:
        fresh_db()
        conn = connect()
        before = state(conn, "ST-WEST-02")
        assert before["hist"] == 1 and before["current"] == 1
        r = rebind.apply_rebind(
            conn, "ST-WEST-02", "ST-WEST-02R", "半迁移演练", simulate_fault=point
        )
        assert not r["ok"] and r["rolled_back"], (point, r)
        assert r["error"]["type"] == "simulated_fault", r["error"]
        # 旧键原样（hist 归属未丢、镜仍在）
        assert state(conn, "ST-WEST-02") == before, (point, state(conn, "ST-WEST-02"))
        # 新键干净：无 hist/镜，连 stores 注册也随事务回滚
        assert state(conn, "ST-WEST-02R") == {"hist": 0, "current": 0, "is_current": 0}
        assert not conn.execute(
            "SELECT 1 FROM stores WHERE id='ST-WEST-02R'"
        ).fetchone(), (point, "stores 新键残留")
        # 映射未落库
        assert rebind.list_mappings(conn) == [], point
        # 回滚后键对核证：无映射记录，呈现原始占位
        vp = r["verify"]
        assert vp["run_id"] is None and vp["old_state"]["hist"] == 1
        conn.close()
        print(f"ok 半迁移注入@{point} → 整笔回滚（旧原样/新干净/无映射）")


def test_verify_flags_dual_mirror():
    """禁态检测：人为制造双键双镜/旧镜残留，核证必须报失败。"""
    fresh_db()
    conn = connect()
    r = rebind.apply_rebind(conn, "ST-EAST-02", "ST-EAST-02R")
    assert r["ok"]
    # 人为塞回一面旧键当前镜（禁态：只改主键留旧镜 / 双键双镜）
    conn.execute(
        """INSERT INTO dim_store_hours_current(
          store_id, open_time, close_time, weekday_mask, biz_status,
          hist_id, as_of_ts, row_hash
        ) VALUES ('ST-EAST-02','10:00','22:00','1111110','open',
                  'HIST-GHOST','2026-11-20T00:00:00Z','ghost')"""
    )
    conn.commit()
    v = rebind.verify(conn)
    assert v["failed"] == 1, v
    checks = v["items"][0]["checks"]
    assert not checks["旧键清空"] and not checks["无双键双镜"], checks
    assert not v["items"][0]["valid"]
    conn.close()
    print("ok 核证检出双键双镜/旧镜残留禁态")


def test_fixtures():
    """夹具：正常改绑一键通过；半迁移夹具整笔回滚。"""
    fresh_db()
    conn = connect()
    r1 = rebind.run_fixture(conn, "rebind_store_key.json")
    assert r1["ok"] and r1["results"][0]["ok"], r1
    assert state(conn, "ST-EAST-02")["hist"] == 0
    assert state(conn, "ST-EAST-02R")["hist"] == 1
    r2 = rebind.run_fixture(conn, "rebind_half_migrate.json")
    assert not r2["ok"], r2
    entry = r2["results"][0]
    assert not entry["ok"] and entry["rolled_back"]
    assert state(conn, "ST-WEST-02") == {"hist": 1, "current": 1, "is_current": 1}
    assert state(conn, "ST-WEST-02R")["hist"] == 0
    # 只有第一笔映射落库
    maps = rebind.list_mappings(conn)
    assert len(maps) == 1 and maps[0]["new_store_id"] == "ST-EAST-02R", maps
    v = rebind.verify(conn)
    assert v["checked"] == 1 and v["failed"] == 0, v
    conn.close()
    print("ok 夹具：一键改绑生效 + 半迁移夹具整笔回滚")


def test_guards():
    """入参护栏：同键/空键/未知旧键/未知故障点。"""
    fresh_db()
    conn = connect()
    r = rebind.apply_rebind(conn, "ST-EAST-01", "ST-EAST-01")
    assert not r["ok"] and r["error"]["type"] == "bad_input"
    r = rebind.apply_rebind(conn, "", "ST-X")
    assert not r["ok"] and r["error"]["type"] == "bad_input"
    r = rebind.apply_rebind(conn, "ST-NOPE", "ST-X")
    assert not r["ok"] and r["error"]["type"] == "not_found"
    r = rebind.apply_rebind(conn, "ST-EAST-01", "ST-X", simulate_fault="boom")
    assert not r["ok"] and r["error"]["type"] == "bad_input"
    assert rebind.list_mappings(conn) == []
    conn.close()
    print("ok 入参护栏（同键/空键/未知旧键/未知故障点）")


if __name__ == "__main__":
    test_happy_rebind()
    test_segment_inheritance()
    test_conflict_rolls_back()
    test_half_migration_fault_points()
    test_verify_flags_dual_mirror()
    test_fixtures()
    test_guards()
    print("\n全部验收通过 ✔")
