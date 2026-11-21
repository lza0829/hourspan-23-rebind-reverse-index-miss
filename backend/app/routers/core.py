from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import STUBS
from app.modules import business_key_rebind as rebind
from app.services import catalog, detect, ingest, scd

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = catalog.overview(conn)
        data["空桩"] = STUBS
        return data
    finally:
        conn.close()


@router.get("/stores")
def stores():
    conn = connect()
    try:
        return catalog.store_board(conn)
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(batch_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY as_of_ts, store_id",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_snapshots ORDER BY as_of_ts DESC, store_id LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/batches")
def batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_batches ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/history")
def history(store_id: str | None = None):
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/current")
def current_mirror():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dim_store_hours_current ORDER BY store_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/detect-runs")
def detect_runs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM detect_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/proofs")
def proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scd_proofs ORDER BY id DESC LIMIT 80"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = Body(None)):
    name = (body or {}).get("fixture", "ingest_change_batch.json")
    return ingest.run_batch(name)


@router.post("/detect/run")
def run_detect(body: dict | None = Body(None)):
    batch_id = (body or {}).get("batch_id")
    return detect.run_detect(batch_id)


@router.post("/scd/apply")
def run_scd(body: dict = Body(...)):
    run_id = body.get("detect_run_id")
    if not run_id:
        raise HTTPException(400, "需要 detect_run_id")
    return scd.apply_type2(run_id)


@router.post("/proofs/rebuild")
def rebuild_proofs():
    return scd.rebuild_proofs()


# ---- 业务键改绑：维键重映射管道（非运营改名） ----

_REBIND_ERR_HTTP = {
    "bad_input": 400,
    "not_found": 404,
    "conflict": 409,
    "simulated_fault": 200,  # 演练：回滚成功即达标，正常返回便于前端展示核证
}


@router.post("/rebind/apply")
def rebind_apply(body: dict = Body(...)):
    """改绑入口：old→new 一笔事务；冲突整次回滚（409），禁止半迁移。"""
    conn = connect()
    try:
        result = rebind.apply_rebind(
            conn,
            body.get("old_store_id"),
            body.get("new_store_id"),
            body.get("reason", ""),
            body.get("simulate_fault"),
        )
    finally:
        conn.close()
    if result["ok"]:
        return result
    status = _REBIND_ERR_HTTP.get(result["error"]["type"], 500)
    if status == 200:
        return result
    raise HTTPException(status, result["error"]["detail"])


@router.post("/rebind/run-fixture")
def rebind_run_fixture(body: dict | None = Body(None)):
    """一键跑改绑夹具；故意半迁移夹具条目须整笔回滚。"""
    name = (body or {}).get("fixture", "rebind_store_key.json")
    conn = connect()
    try:
        result = rebind.run_fixture(conn, name)
    finally:
        conn.close()
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@router.get("/rebind/mappings")
def rebind_mappings():
    """映射审计：键迁移表全量（跑次/old/new/时刻/段数）。"""
    conn = connect()
    try:
        return rebind.list_mappings(conn)
    finally:
        conn.close()


@router.get("/rebind/verify")
def rebind_verify(old_store_id: str | None = None, new_store_id: str | None = None):
    """键迁移核证（只读）：old/new、段数继承、旧键清空位、双键双镜禁态。"""
    conn = connect()
    try:
        return rebind.verify(conn, old_store_id, new_store_id)
    finally:
        conn.close()


@router.get("/settings")
def settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


@router.get("/stubs")
def stubs():
    return {"stubs": STUBS}

# --- plant routes ---
from app.modules import rebind_reverse_index as rebind_idx

@router.get("/rebind/reverse-index")
def rebind_reverse_index(old_store_id: str | None = None):
    conn = connect()
    try:
        if old_store_id:
            return rebind_idx.lookup_old(conn, old_store_id)
        return rebind_idx.list_index(conn)
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "store_id" in out and "biz_key" not in out:
        out["biz_key"] = out.get("store_id")
    if "hist_id" in out and "segment_ref" not in out:
        out["segment_ref"] = out.get("hist_id")
    if "row_hash" in out and "content_digest" not in out:
        out["content_digest"] = out.get("row_hash")
    if "aligned" not in out:
        out["aligned"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True, "scope": "hourspan"}
    if extra:
        data.update(extra)
    return data

