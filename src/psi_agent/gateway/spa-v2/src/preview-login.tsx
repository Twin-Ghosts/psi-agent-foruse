/**
 * 登录界面预览（仅供本地肉眼验收，不进生产）。
 *
 * 用假数据 mock 掉 /auth/* 接口，这样无需真实 Gateway/云端后端，也能在浏览器里
 * 把原型 12 屏全部点一遍。右上角切换场景。
 */
import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import HubLoginPanel from './components/user-hub/HubLoginPanel'
import './styles/globals.css'
import './components/user-hub/user-hub.css'

type Scenario = 'flow' | 'loggedIn' | 'badCode' | 'rateLimit' | 'offline'

let scenario: Scenario = 'flow'
let sentOnce = false

const realFetch = window.fetch.bind(window)

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

// 拦截 /auth/* ，其余照常
window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = url.replace(/^https?:\/\/[^/]+/, '')
  if (!path.startsWith('/auth')) return realFetch(input as RequestInfo, init)

  await new Promise((r) => setTimeout(r, 320)) // 模拟网络延迟，看得到 loading

  if (scenario === 'offline') return Promise.reject(new Error('network down'))

  // 状态探测
  if (path === '/auth/status') {
    const loggedIn = scenario === 'loggedIn'
    return json({
      endpoint: 'https://auth.example.com',
      loggedIn,
      deviceKey: 'dev-preview',
      platform: 'win32',
      credentialEncrypted: true,
    })
  }
  if (path === '/auth/me') {
    return json({
      user: { id: 'u_demo', displayName: '海豚用户 8000', avatarUrl: null, createdAt: '2026-08-11' },
      identities: [{ provider: 'phone', identifier: '13800138000', verifiedAt: '2026-08-11' }],
    })
  }
  if (path === '/auth/devices') {
    return json({
      devices: [
        { id: 'd1', platform: 'Windows', name: 'ZSD-WORKSTATION', createdAt: '2026-08-11', lastSeenAt: '刚刚', current: true },
        { id: 'd2', platform: 'macOS', name: 'MacBook Pro', createdAt: '2026-08-10', lastSeenAt: '2 小时前', current: false },
        { id: 'd3', platform: 'Windows', name: 'DESKTOP-A19F', createdAt: '2026-08-08', lastSeenAt: '3 天前', current: false },
      ],
    })
  }
  if (path === '/auth/send-code') {
    if (scenario === 'rateLimit') return json({ error: 'rate_limited', retryAfter: 48 }, 429)
    sentOnce = true
    return json({ retryAfter: 60 })
  }
  if (path === '/auth/verify') {
    if (scenario === 'badCode') return json({ error: 'invalid_code', remaining: 3 }, 401)
    // 正常流程：新用户 → tempToken，走 A3 建号
    return json({ tempToken: 'tmp_demo', isNewUser: true })
  }
  if (path === '/auth/complete') return json({ token: 'tok_demo', user: { id: 'u_demo' } })
  if (path === '/auth/logout') return json({ ok: true })
  if (path.startsWith('/auth/devices/')) return json({ ok: true })
  return json({ error: 'not_found' }, 404)
}

void sentOnce

const SCENARIOS: { key: Scenario; label: string; hint: string }[] = [
  { key: 'flow', label: '正常流程', hint: 'A1→A2→A3：输入→验证码(填 6 位自动提交)→建号' },
  { key: 'loggedIn', label: '已登录', hint: 'C1 账户面板 →「管理登录设备」进 C2' },
  { key: 'badCode', label: '验证码错误', hint: 'D1：填满 6 位后整体转红+剩余次数' },
  { key: 'rateLimit', label: '发码限频', hint: 'D2：点「获取验证码」触发 429 警告条' },
  { key: 'offline', label: '断网', hint: 'D3：logo 灰度 + 重试' },
]

function Preview() {
  const [sc, setSc] = useState<Scenario>('flow')
  const [key, setKey] = useState(0)
  const pick = (s: Scenario) => {
    scenario = s
    sentOnce = false
    setSc(s)
    setKey((k) => k + 1) // 换 key 强制重挂载，触发 getAuthStatus 重新探测
  }
  return (
    <div style={{ minHeight: '100vh', background: '#eef4ff', padding: 20 }}>
      <div style={{ maxWidth: 720, margin: '0 auto' }}>
        <h2 style={{ font: '700 18px system-ui', color: '#0c2340' }}>登录界面预览</h2>
        <p style={{ font: '13px system-ui', color: '#5a7190' }}>
          点下面切换场景。这是用假数据本地预览，不连真实后端。当前：
          <b>{SCENARIOS.find((x) => x.key === sc)?.hint}</b>
        </p>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '12px 0 8px' }}>
          {SCENARIOS.map((s) => (
            <button
              key={s.key}
              onClick={() => pick(s.key)}
              style={{
                padding: '7px 14px',
                borderRadius: 999,
                border: sc === s.key ? '1px solid #007bff' : '1px solid #c5dcff',
                background: sc === s.key ? '#007bff' : '#fff',
                color: sc === s.key ? '#fff' : '#0050ad',
                font: '650 13px system-ui',
                cursor: 'pointer',
              }}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>
      <HubLoginPanel key={key} show onClose={() => pick(sc)} />
    </div>
  )
}

const root = document.getElementById('app')
if (root) createRoot(root).render(<StrictMode><Preview /></StrictMode>)
