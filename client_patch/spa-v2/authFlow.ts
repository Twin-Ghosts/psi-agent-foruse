/** 登录面板的纯逻辑：校验、错误码文案、两段式判断、倒计时。
 *
 * 单独成文件而非写在 HubLoginPanel.tsx 里，是为了能被 vitest 直接测 —— 组件里
 * 的这些判断如果只能靠点界面来验，就没人会验。仓库里 services/*.test.ts 是同一
 * 套做法。
 */

/** 云端错误码 → 中文文案。未收录的码原样透出，避免"未知错误"吞掉线索。 */
export const AUTH_ERROR_TEXT: Record<string, string> = {
  invalid_phone: '手机号格式不正确',
  invalid_email: '邮箱格式不正确',
  invalid_code: '验证码不正确',
  code_expired: '验证码已过期或不存在，请重新获取',
  temp_token_invalid: '注册凭证已过期，请重新验证',
  unauthorized: '登录态已失效，请重新登录',
  invitation_required: '当前需要邀请码',
  invitation_invalid: '邀请码无效或已被使用',
  rate_limited: '操作过于频繁，请稍后再试',
  provider_error: '短信/邮件服务暂时不可用，请稍后再试',
  upstream_unreachable: '连不上认证服务，请检查网络或稍后再试',
  auth_endpoint_not_configured: '本机未配置认证服务地址',
  phone_or_email_required: '请填写手机号或邮箱',
  code_required: '请填写验证码',
}

export function humanizeAuthError(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err ?? '')
  if (!raw) return '操作失败，请重试'
  return AUTH_ERROR_TEXT[raw] ?? raw
}

/** 大陆手机号：与服务端 ^1[3-9]\d{9}$ 同规则，避免前端放过、后端才拒。 */
const PHONE_RE = /^1[3-9]\d{9}$/

export function isValidPhone(v: string): boolean {
  return PHONE_RE.test(v.trim())
}

export function isValidEmail(v: string): boolean {
  const s = v.trim()
  if (s.split('@').length !== 2 || /\s/.test(s)) return false
  const [local, domain] = s.split('@')
  return Boolean(local) && domain.includes('.') && !domain.startsWith('.') && !domain.endsWith('.')
}

export type Channel = 'phone' | 'email'

/** 发码前的本地校验。返回错误文案，空串表示通过。 */
export function validateAccount(channel: Channel, value: string): string {
  if (channel === 'phone') {
    return isValidPhone(value) ? '' : '请输入 11 位大陆手机号'
  }
  return isValidEmail(value) ? '' : '请输入有效的邮箱地址'
}

/** 只保留数字并截到 6 位 —— 用户粘贴带空格的验证码很常见。 */
export function normalizeCode(raw: string): string {
  return raw.replace(/\D/g, '').slice(0, 6)
}

/** 服务端给的 retryAfter 优先；缺失或非正数时回落到 60 秒。 */
export function cooldownFrom(retryAfter: unknown): number {
  return typeof retryAfter === 'number' && retryAfter > 0 ? retryAfter : 60
}

/** 校验响应是否要求走两段式注册（新用户）。 */
export function needsComplete(res: { token?: string; tempToken?: string }): boolean {
  return Boolean(res.tempToken) && !res.token
}

/** 只有拿到 tempToken 才算新用户；有 token 就是老用户直接登录。 */
export function isNewUser(res: { token?: string; tempToken?: string; isNewUser?: boolean }): boolean {
  return needsComplete(res) || Boolean(res.isNewUser && !res.token)
}
