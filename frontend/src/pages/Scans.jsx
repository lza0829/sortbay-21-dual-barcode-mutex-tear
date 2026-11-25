import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Scans() {
  const [rows, setRows] = useState([])
  const [barcode, setBarcode] = useState('')
  const [dest, setDest] = useState('110')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [reject, setReject] = useState(null)
  const [replayCode, setReplayCode] = useState('')
  const [replay, setReplay] = useState(null)
  const load = () => api('/扫描').then(setRows).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  const create = async () => {
    setErr(''); setMsg(''); setReject(null)
    try {
      const r = await api('/扫描', { method: 'POST', body: JSON.stringify({ 条码: barcode, 目的地码: dest }) })
      setMsg(`过机装载成功，扫描编号 ${r.id}`); setBarcode(''); load()
    } catch (e) {
      // 互斥单行臂：409 结构化拒单，区别于普通错误提示
      if (e && e.拒因码 === 'duplicate_in_shift') {
        setReject(e)
      } else {
        setErr(e.message)
      }
    }
  }
  const routeOne = async (id) => {
    setErr(''); setMsg(''); setReject(null)
    try {
      await api(`/扫描/${id}/路由`, { method: 'POST' })
      setMsg('路由成功'); load()
    } catch (e) { setErr(e.message) }
  }
  const doReplay = async () => {
    if (!replayCode.trim()) return
    setReplay(null); setErr('')
    try {
      const q = new URLSearchParams({ 条码: replayCode.trim() }).toString()
      setReplay(await api(`/扫描/重放?${q}`))
    } catch (e) { setErr(e.message) }
  }
  return (
    <div className="panel">
      <h2>过机扫描 <span className="tag">互斥单行臂 · ADR-0001</span></h2>
      <p className="muted">同班次同条码至多一行；再装载整笔失败（409），不新增行、不改既有态，拒收留审计可重放。</p>
      <div className="row">
        <label>条码<input value={barcode} onChange={(e) => setBarcode(e.target.value)} /></label>
        <label>目的地码<input value={dest} onChange={(e) => setDest(e.target.value)} /></label>
        <button type="button" onClick={create}>装载扫描</button>
      </div>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{err}</div>}
      {reject && (
        <div className="err reject-card">
          <div><strong>再扫拒收（{reject.拒因码}）</strong></div>
          <div>{reject.明细}</div>
          <div className="muted">
            条码 {reject.条码} · 班次 {reject.班次} · 既有扫描编号 {reject.既有扫描编号} · 既有状态 {reject.既有状态}
          </div>
        </div>
      )}
      <table>
        <thead><tr><th>编号</th><th>条码</th><th>目的地</th><th>状态</th><th>口门</th><th>操作</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td><td>{r.barcode}</td><td>{r.dest_code}</td><td>{r.status}</td><td>{r.chute_id || '-'}</td>
              <td>{(r.status === '待路由' || r.status === '错分回流') && <button type="button" onClick={() => routeOne(r.id)}>路由</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>条码命运重放</h3>
      <div className="row">
        <label>条码<input value={replayCode} onChange={(e) => setReplayCode(e.target.value)} /></label>
        <button type="button" className="secondary" onClick={doReplay}>重放</button>
      </div>
      {replay && (
        <div className="replay">
          <div className="muted">{replay.口径} · 班次 {replay.班次} · 扫描行数 {replay.扫描行数} · 再扫拒收 {replay.再扫拒收次数} 次</div>
          <table>
            <thead><tr><th>扫描编号</th><th>状态</th><th>口门</th><th>裁决事件</th><th>审计动作</th></tr></thead>
            <tbody>
              {replay.扫描.map((s) => (
                <tr key={s.id}>
                  <td>{s.id}</td><td>{s.status}</td><td>{s.chute_id || '-'}</td>
                  <td>{replay.裁决事件.filter((e) => e.scan_id === s.id).map((e) => e.action).join('、') || '-'}</td>
                  <td>{replay.审计流水.filter((a) => a.ref_id === s.id).map((a) => a.action).join('、') || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
