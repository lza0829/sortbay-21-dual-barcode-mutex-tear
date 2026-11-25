import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Audit() {
  const [rows, setRows] = useState([])
  const [action, setAction] = useState('')
  const [barcode, setBarcode] = useState('')
  const [err, setErr] = useState('')
  const load = async () => {
    const p = new URLSearchParams()
    if (action) p.set('动作', action)
    if (barcode.trim()) p.set('条码', barcode.trim())
    const qs = p.toString()
    setRows(await api(`/审计${qs ? `?${qs}` : ''}`))
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  return (
    <div className="panel">
      <h2>审计流水</h2>
      <p className="muted">「再扫拒收」记录每一次同班次同条码的失败再装载，可按条码重放某件命运。</p>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <label>动作
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">全部</option>
            <option value="过机装载">过机装载</option>
            <option value="再扫拒收">再扫拒收</option>
            <option value="路由裁决">路由裁决</option>
          </select>
        </label>
        <label>条码<input value={barcode} onChange={(e) => setBarcode(e.target.value)} placeholder="按条码过滤" /></label>
        <button type="button" onClick={() => load().catch((e) => setErr(e.message))}>查询</button>
      </div>
      <table>
        <thead><tr><th>编号</th><th>类别</th><th>引用</th><th>动作</th><th>时间</th><th>备注/条码</th></tr></thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id} className={a.action === '再扫拒收' ? 'row-reject' : ''}>
              <td>{a.id}</td><td>{a.kind}</td><td>{a.ref_id ?? '-'}</td>
              <td>{a.action}</td><td>{a.created_at}</td><td>{a.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
