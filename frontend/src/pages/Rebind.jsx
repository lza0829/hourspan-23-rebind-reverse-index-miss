import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const CHECK_ORDER = ["旧键清空", "段数继承", "镜已切换", "无双键双镜", "旧键反查可查"];

export default function Rebind() {
  const [oldId, setOldId] = useState("");
  const [newId, setNewId] = useState("");
  const [reason, setReason] = useState("");
  const [mappings, setMappings] = useState([]);
  const [proof, setProof] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () =>
    Promise.all([api("/api/rebind/mappings"), api("/api/rebind/verify")])
      .then(([m, v]) => { setMappings(m); setProof(v); })
      .catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reload(); }, []);

  const show = async (p) => {
    setErr(""); setMsg("");
    try {
      const r = await p();
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
      await reload();
    }
  };

  const apply = () =>
    show(() =>
      api("/api/rebind/apply", {
        method: "POST",
        body: JSON.stringify({
          old_store_id: oldId.trim(),
          new_store_id: newId.trim(),
          reason: reason.trim(),
        }),
      })
    );

  const runFixture = (fixture) =>
    show(() =>
      api("/api/rebind/run-fixture", {
        method: "POST",
        body: JSON.stringify({ fixture }),
      })
    );

  const inputStyle = {
    minWidth: 170, padding: "0.4rem 0.6rem", borderRadius: 8,
    border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)",
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>键改绑</h1>
          <p>
            维键重映射管道：一次 store_id 重绑 old→new，hist 全段、当前镜与旧键反查索引
            同事务过户（缺一回滚）；旧键清空、新键继承段数，冲突整次回滚，禁止双键双镜。
            路由架构：反查索引为权威——命中 old 经反查转 new，缺反查则旧键请求失败（见核证页）。
          </p>
        </div>
      </div>

      <div className="panel">
        <h2>改绑入口</h2>
        <div className="actions">
          <input value={oldId} onChange={(e) => setOldId(e.target.value)}
            placeholder="旧键，如 ST-EAST-02" style={inputStyle} />
          <span className="muted">→</span>
          <input value={newId} onChange={(e) => setNewId(e.target.value)}
            placeholder="新键，如 ST-EAST-02R" style={inputStyle} />
          <input value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="改绑事由（入映射审计）" style={{ ...inputStyle, minWidth: 220 }} />
          <button onClick={apply}>执行改绑</button>
          <button className="secondary" onClick={() => runFixture("rebind_store_key.json")}>
            跑改绑夹具
          </button>
          <button className="secondary" onClick={() => runFixture("rebind_half_migrate.json")}>
            半迁移演练夹具
          </button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>

      <div className="panel">
        <h2>映射审计 · store_key_rebinds</h2>
        <table>
          <thead>
            <tr>
              <th>跑次</th><th>旧键</th><th>新键</th><th>迁移段数</th><th>带镜</th>
              <th>状态</th><th>事由</th><th>时刻</th>
            </tr>
          </thead>
          <tbody>
            {mappings.map((m) => (
              <tr key={m.id}>
                <td className="mono">{m.id}</td>
                <td className="mono">{m.old_store_id}</td>
                <td className="mono">{m.new_store_id}</td>
                <td>{m.moved_hist_count}</td>
                <td>{m.moved_current ? "是" : "否"}</td>
                <td><span className="badge ok">{m.status}</span></td>
                <td>{m.reason || "—"}</td>
                <td>{m.created_at}</td>
              </tr>
            ))}
            {!mappings.length && (
              <tr><td colSpan={8} className="muted">尚无改绑跑次</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>键迁移核证（只读）</h2>
        <p className="muted">
          已核 {proof?.checked ?? 0} 笔 / 失败 {proof?.failed ?? 0} 笔。
          旧键清空位 = 旧键名下无 hist、无 is_current、无 current；段数继承 = 新键段数 ≥ 迁移段数且旧键零遗留。
        </p>
        <table>
          <thead>
            <tr>
              <th>跑次</th><th>old → new</th>
              <th>旧键占位(hist/镜)</th><th>新键占位(hist/镜)</th>
              {CHECK_ORDER.map((c) => <th key={c}>{c}</th>)}
              <th>结论</th>
            </tr>
          </thead>
          <tbody>
            {(proof?.items || []).map((it, i) => (
              <tr key={it.run_id || i}>
                <td className="mono">{it.run_id || "—"}</td>
                <td className="mono">{it.old_store_id} → {it.new_store_id}</td>
                <td>{it.old_state.hist} / {it.old_state.current}</td>
                <td>{it.new_state.hist} / {it.new_state.current}</td>
                {CHECK_ORDER.map((c) => (
                  <td key={c}>
                    {it.checks ? (
                      <span className={`badge ${it.checks[c] ? "ok" : "bad"}`}>
                        {it.checks[c] ? "过" : "违"}
                      </span>
                    ) : "—"}
                  </td>
                ))}
                <td>
                  {it.valid === null ? (
                    <span className="muted">{it.note}</span>
                  ) : (
                    <span className={`badge ${it.valid ? "ok" : "bad"}`}>
                      {it.valid ? "闭环" : "破环"}
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {proof && !proof.items.length && (
              <tr><td colSpan={10} className="muted">无映射可核</td></tr>
            )}
          </tbody>
        </table>
        <div className="actions" style={{ marginTop: "0.85rem" }}>
          <button className="secondary" onClick={reload}>刷新核证</button>
        </div>
      </div>
    </>
  );
}
