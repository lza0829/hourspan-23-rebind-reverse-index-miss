import React, { useEffect, useState } from "react";
import { api, rebindReverseIndex, rebindReverseVerify, rebindRoute } from "../api.js";

const RIDX_CHECKS = ["反查归属一致", "旧键无旧镜", "段数吻合", "邻键未串改"];

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [indexList, setIndexList] = useState(null);
  const [ridxProof, setRidxProof] = useState(null);
  const [routeKey, setRouteKey] = useState("");
  const [routeResult, setRouteResult] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () => {
    api("/api/proofs").then(setRows).catch((e) => setErr(String(e.message || e)));
    rebindReverseIndex()
      .then(setIndexList)
      .catch((e) => { setIndexList(null); setErr(String(e.message || e)); });
    rebindReverseVerify()
      .then(setRidxProof)
      .catch((e) => { setRidxProof(null); setErr(String(e.message || e)); });
  };

  useEffect(() => { reload(); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const probeRoute = async () => {
    setRouteResult(null);
    setErr("");
    try {
      const r = await rebindRoute(routeKey.trim());
      setRouteResult({ ok: true, data: r });
    } catch (e) {
      // 架构甲：缺反查的旧键请求在此失败（409 reverse_index_missing）
      setRouteResult({ ok: false, error: String(e.message || e) });
    }
  };

  return (
    <>
      <div data-plant="23-rebind-reverse-index-miss" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>
            当前镜 hist_id / row_hash 必须与 is_current 历史行一致；旧键反查索引为路由权威
            （架构甲：命中 old 经反查转 new，缺反查则旧键请求失败），失败须可观测。
          </p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建核证</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>

      <div className="panel">
        <h2>旧键路由点查（路由权威）</h2>
        <p className="muted">
          输入任意业务键：命中已改绑旧键必须经反查转到新键；缺反查且库内已空的旧键请求直接失败。
        </p>
        <div className="actions">
          <input
            value={routeKey}
            onChange={(e) => setRouteKey(e.target.value)}
            placeholder="业务键，如 ST-EAST-02"
            style={{ minWidth: 220, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={probeRoute}>解析路由</button>
        </div>
        {routeResult && (
          <pre className="msg">
            {routeResult.ok
              ? JSON.stringify(routeResult.data, null, 2)
              : `路由拒绝：${routeResult.error}`}
          </pre>
        )}
      </div>

      <div className="panel">
        <h2>
          旧键反查核证（只读）
          {ridxProof && (
            <span className="muted" style={{ marginLeft: 10 }}>
              已核 {ridxProof.checked} 笔 · 失败 {ridxProof.failed} 笔 · 架构 {ridxProof.architecture}
            </span>
          )}
        </h2>
        <table>
          <thead>
            <tr>
              <th>跑次</th><th>old → new</th><th>迁移段数</th><th>带镜</th><th>时刻</th>
              {RIDX_CHECKS.map((c) => <th key={c}>{c}</th>)}
              <th>结论</th>
            </tr>
          </thead>
          <tbody>
            {(ridxProof?.items || []).map((it) => (
              <tr key={it.run_id}>
                <td className="mono">{it.run_id}</td>
                <td className="mono">{it.old_store_id} → {it.new_store_id}</td>
                <td>{it.migrated_segments}</td>
                <td>{it.moved_current ? "是" : "否"}</td>
                <td>{it.created_at}</td>
                {RIDX_CHECKS.map((c) => (
                  <td key={c}>
                    <span className={`badge ${it.checks[c] ? "ok" : "bad"}`}>
                      {it.checks[c] ? "过" : "违"}
                    </span>
                  </td>
                ))}
                <td>
                  <span className={`badge ${it.valid ? "ok" : "bad"}`}>
                    {it.valid ? "闭环" : "破环"}
                  </span>
                </td>
              </tr>
            ))}
            {ridxProof && !ridxProof.items.length && (
              <tr><td colSpan={10} className="muted">无反查行可核</td></tr>
            )}
          </tbody>
        </table>
        {!!ridxProof?.missing_entries?.length && (
          <>
            <h3 style={{ marginTop: "1rem" }}>反查缺行（迁移已成、旧键反查未接）</h3>
            <table>
              <thead><tr><th>旧键</th><th>类型</th><th>说明</th></tr></thead>
              <tbody>
                {ridxProof.missing_entries.map((m) => (
                  <tr key={m.store_id}>
                    <td className="mono">{m.store_id}</td>
                    <td><span className="badge bad">{m.kind}</span></td>
                    <td className="muted">{m.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>

      <div className="panel">
        <h2>旧键反查索引 · store_key_rebinds（按 old 查 new）</h2>
        <table>
          <thead>
            <tr><th>旧键</th><th>新键</th><th>跑次</th><th>迁移段数</th><th>带镜</th><th>时刻</th></tr>
          </thead>
          <tbody>
            {(indexList?.entries || []).map((e) => (
              <tr key={e.run_id}>
                <td className="mono">{e.old_store_id}</td>
                <td className="mono">{e.new_store_id}</td>
                <td className="mono">{e.run_id}</td>
                <td>{e.migrated_segments}</td>
                <td>{e.moved_current ? "是" : "否"}</td>
                <td>{e.created_at}</td>
              </tr>
            ))}
            {indexList && !indexList.entries.length && (
              <tr><td colSpan={6} className="muted">反查索引为空</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>scd_proofs</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
