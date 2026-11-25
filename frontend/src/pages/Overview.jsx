import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Overview() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/总览').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中…</div>
  const d = data.分布 || {}
  const r = data.对账 || {}
  const p = data.互斥探针 || {}
  return (
    <div className="panel">
      <h2>总览 <span className="tag">{data.口径 || ''}</span></h2>
      <p className="muted">当前班次：{data.班次?.business_day} {data.班次?.name} · {data.班次?.sealed ? '已封班' : '未封班'}</p>
      <div className="grid">
        {Object.entries(d).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
      </div>
      <h3>互斥臂对账（由扫描册行数与成功灯重放）</h3>
      <table>
        <tbody>
          <tr><td>扫描册行数</td><td>{r.扫描册行数}</td></tr>
          <tr><td>各状态桶之和</td><td>{r.各状态桶之和}</td></tr>
          <tr><td>册桶一致</td><td>{r.册桶一致 ? '是' : '否（未闭合）'}</td></tr>
          <tr><td>成功灯数</td><td>{r.成功灯数}（灯册一致：{r.灯册一致 ? '是' : '否'}）</td></tr>
          <tr><td>同码多行数</td><td>{r.同码多行数}</td></tr>
          <tr><td>无行成功灯数</td><td>{r.无行成功灯数}</td></tr>
          <tr><td>皮带双挂数</td><td>{r.皮带双挂数}</td></tr>
          <tr>
            <td>互斥臂闭合</td>
            <td className={r.互斥臂闭合 ? 'ok' : 'err-text'}>{r.互斥臂闭合 ? '闭合' : '未闭合（双破唯一）'}</td>
          </tr>
          <tr><td>再扫拒收次数</td><td>{r.再扫拒收次数}（拒收不进任何桶）</td></tr>
        </tbody>
      </table>
      <h3>占用探针（每码一盏灯、一行册、一占用）</h3>
      <table>
        <thead><tr><th>条码</th><th>占用行数</th><th>状态</th><th>口门</th><th>扫描行</th></tr></thead>
        <tbody>
          {(p.占用灯 || []).map((o) => (
            <tr key={o.barcode} className={o.占用行数 === 1 ? '' : 'row-reject'}>
              <td>{o.barcode}</td><td>{o.占用行数}</td><td>{o.status}</td>
              <td>{o.chute_ids.length ? o.chute_ids.join(',') : '-'}</td>
              <td>{o.ids.join(',')}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table>
        <thead><tr><th>口门</th><th>标题</th><th>状态</th><th>在口件数</th></tr></thead>
        <tbody>
          {(data.口门 || []).map((c) => (
            <tr key={c.id}><td>{c.code}</td><td>{c.title}</td><td>{c.status}</td><td>{c.在口件数}</td></tr>
          ))}
        </tbody>
      </table>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
