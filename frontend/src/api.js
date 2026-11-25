export async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const text = await res.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { data = { 明细: text } }
  if (!res.ok) {
    // 保留结构化拒单（如 409 duplicate_in_shift），把响应体字段挂到错误对象上
    const msg = (data && (data.detail || data.明细)) || res.statusText
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    err.status = res.status
    if (data && typeof data === 'object') Object.assign(err, data)
    throw err
  }
  return data
}
