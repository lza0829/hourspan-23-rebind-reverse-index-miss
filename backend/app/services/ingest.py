import json
from uuid import uuid4

from app.db import FIXTURES_DIR, connect
from app.modules import rebind_reverse_index as rebind_idx
from app.seed import now_iso, row_hash


def run_batch(fixture_name: str = "ingest_change_batch.json"):
    path = FIXTURES_DIR / fixture_name
    if not path.exists():
        return {"batch_id": None, "accepted": 0, "message": "无夹具"}
    data = json.loads(path.read_text(encoding="utf-8"))
    snaps = data.get("snapshots", [])
    batch_id = f"B-{uuid4().hex[:8]}"
    now = now_iso()
    conn = connect()
    accepted = 0
    skipped = 0
    routed = 0
    rejected = []
    try:
        for s in snaps:
            raw_store_id = s["store_id"]
            # 架构甲：入档遇到旧键，反查索引为路由权威——
            # 命中 old 必须改写到 new；缺反查则该旧键请求失败，禁止写到旧镜。
            try:
                route = rebind_idx.resolve_route(conn, raw_store_id)
            except rebind_idx.ReverseIndexError as exc:
                rejected.append(
                    {"store_id": raw_store_id, "type": exc.err_type, "detail": exc.detail}
                )
                continue
            store_id = route["authoritative"]
            if route["rerouted"]:
                routed += 1
            store = conn.execute("SELECT id FROM stores WHERE id=?", (store_id,)).fetchone()
            if not store:
                skipped += 1
                continue
            ot, ct = s.get("open_time"), s.get("close_time")
            wm = s.get("weekday_mask", "1111100")
            st = s.get("biz_status", "open")
            rh = row_hash(ot, ct, wm, st)
            snap_id = s.get("id") or f"SNAP-{uuid4().hex[:10]}"
            conn.execute(
                """INSERT OR IGNORE INTO source_snapshots(
                  id, batch_id, store_id, as_of_ts, open_time, close_time,
                  weekday_mask, biz_status, payload_json, row_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    snap_id,
                    batch_id,
                    store_id,
                    s["as_of_ts"],
                    ot,
                    ct,
                    wm,
                    st,
                    json.dumps(
                        {
                            **s.get("payload", {}),
                            **(
                                {
                                    "rebind_routed_from": raw_store_id,
                                    "rebind_run_id": route["run_id"],
                                }
                                if route["rerouted"]
                                else {}
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    rh,
                ),
            )
            if conn.total_changes:
                accepted += 1
            else:
                skipped += 1
        report = {
            "accepted": accepted,
            "skipped": skipped,
            "routed_old_keys": routed,
            "rejected": rejected,
            "total": len(snaps),
        }
        conn.execute(
            "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, now, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"batch_id": batch_id, **report}
    finally:
        conn.close()
