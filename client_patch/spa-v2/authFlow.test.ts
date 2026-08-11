import { describe, expect, it } from 'vitest'

import {
  AUTH_ERROR_TEXT,
  cooldownFrom,
  humanizeAuthError,
  isNewUser,
  isValidEmail,
  isValidPhone,
  needsComplete,
  normalizeCode,
  validateAccount,
} from './authFlow'

describe('手机号校验与服务端同规则', () => {
  it('接受大陆号段', () => {
    expect(isValidPhone('13800000000')).toBe(true)
    expect(isValidPhone('19912345678')).toBe(true)
    expect(isValidPhone(' 13800000000 ')).toBe(true)
  })

  it('拒绝非大陆号段与长度不符', () => {
    expect(isValidPhone('12800000000')).toBe(false) // 第二位 2 不在 3-9
    expect(isValidPhone('1380000000')).toBe(false) // 10 位
    expect(isValidPhone('138000000000')).toBe(false) // 12 位
    expect(isValidPhone('')).toBe(false)
    expect(isValidPhone('abcdefghijk')).toBe(false)
  })

  it('不自行去 +86 前缀：归一化是服务端的职责，前端只做格式提示', () => {
    // 若前端偷偷去前缀，用户会以为格式没问题；实际请求体仍带 +86，
    // 反而掩盖问题。这里明确断言前端不做这件事。
    expect(isValidPhone('+8613800000000')).toBe(false)
  })
})

describe('邮箱校验', () => {
  it('接受常见写法', () => {
    expect(isValidEmail('u@example.com')).toBe(true)
    expect(isValidEmail('u.s+tag@ex.co')).toBe(true)
  })

  it('拒绝缺域名/多个 @/空格/点号位置异常', () => {
    for (const bad of ['', 'u@', '@x.com', 'u@x', 'a@b@c.com', 'u x@a.com', 'u@.com', 'u@x.']) {
      expect(isValidEmail(bad)).toBe(false)
    }
  })
})

describe('发码前本地校验', () => {
  it('手机号通道给出手机号专属提示', () => {
    expect(validateAccount('phone', '123')).toContain('手机号')
    expect(validateAccount('phone', '13800000000')).toBe('')
  })

  it('邮箱通道给出邮箱专属提示', () => {
    expect(validateAccount('email', 'nope')).toContain('邮箱')
    expect(validateAccount('email', 'u@example.com')).toBe('')
  })
})

describe('验证码输入归一化', () => {
  it('剔除非数字并截到 6 位', () => {
    expect(normalizeCode('12 34 56')).toBe('123456')
    expect(normalizeCode('1234567890')).toBe('123456')
    expect(normalizeCode('abc123')).toBe('123')
    expect(normalizeCode('')).toBe('')
  })
})

describe('错误码文案', () => {
  it('已知码译成中文', () => {
    expect(humanizeAuthError(new Error('invalid_code'))).toBe('验证码不正确')
    expect(humanizeAuthError(new Error('rate_limited'))).toBe('操作过于频繁，请稍后再试')
  })

  it('未知码原样透出，不吞掉线索', () => {
    expect(humanizeAuthError(new Error('some_new_code'))).toBe('some_new_code')
  })

  it('空错误给出兜底文案', () => {
    expect(humanizeAuthError(new Error(''))).toBe('操作失败，请重试')
    expect(humanizeAuthError(null)).toBe('操作失败，请重试')
  })

  it('契约里的每个错误码都有文案（漏一个用户就会看到英文码）', () => {
    for (const code of [
      'invalid_phone',
      'invalid_email',
      'invalid_code',
      'code_expired',
      'temp_token_invalid',
      'unauthorized',
      'invitation_required',
      'invitation_invalid',
      'rate_limited',
      'provider_error',
    ]) {
      expect(AUTH_ERROR_TEXT[code], `缺少 ${code} 的文案`).toBeTruthy()
    }
  })
})

describe('倒计时以服务端 retryAfter 为准', () => {
  it('用服务端给的秒数', () => {
    expect(cooldownFrom(60)).toBe(60)
    expect(cooldownFrom(15)).toBe(15)
  })

  it('缺失或非法时回落 60 秒，不给 0（否则按钮立刻可再点、必被限频拒）', () => {
    expect(cooldownFrom(undefined)).toBe(60)
    expect(cooldownFrom(0)).toBe(60)
    expect(cooldownFrom(-5)).toBe(60)
    expect(cooldownFrom('60')).toBe(60)
  })
})

describe('两段式注册判断', () => {
  it('只有 tempToken 时需要走 complete', () => {
    expect(needsComplete({ tempToken: 'tt-1' })).toBe(true)
    expect(isNewUser({ tempToken: 'tt-1', isNewUser: true })).toBe(true)
  })

  it('拿到 token 即老用户，不该再调 complete', () => {
    expect(needsComplete({ token: 'tok-1' })).toBe(false)
    expect(isNewUser({ token: 'tok-1' })).toBe(false)
  })

  it('token 与 tempToken 同时存在时按老用户处理（不重复建号）', () => {
    expect(needsComplete({ token: 'tok-1', tempToken: 'tt-1' })).toBe(false)
    expect(isNewUser({ token: 'tok-1', tempToken: 'tt-1', isNewUser: true })).toBe(false)
  })
})
