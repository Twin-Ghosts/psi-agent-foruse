/** 登录面板的渲染冒烟测：真挂载组件，走真实交互，断言屏上文字。
 *
 * 与 authFlow.test.ts 的分工：那边测纯逻辑（分组、打码、归屏），这边测
 * 「屏是否真的渲染出来、按钮是否真的能点到下一屏」。纯逻辑全绿但组件把某屏
 * 接错分支时，只有这一层会红。
 *
 * 后端用 authMock（URL 带 ?authMock=1 时接管 /auth/*），所以不需要起服务。
 *
 * @vitest-environment jsdom
 */

import '@testing-library/jest-dom/vitest'

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MOCK_CODE, SCENARIOS, reset as resetFake } from './__fixtures__/fakeAuthBackend'

/* 用模块替身把 8 个认证接口换成假后端。
 *
 * 关键在于替身只存在于测试进程：生产代码里没有任何文件 import 这个 fixture，
 * 打包器收不到它，固定验证码不会进入产物。（早先的写法是让 api.ts 静态 import
 * 假后端、靠 `?authMock=1` 切换，结果固定码确实被打进了 dist。） */
vi.mock('../../services/api', async () => {
  const real = await vi.importActual<typeof import('../../services/api')>('../../services/api')
  const fake = await import('./__fixtures__/fakeAuthBackend')
  return {
    ...real,
    getAuthStatus: fake.status,
    sendAuthCode: fake.sendCode,
    verifyAuthCode: fake.verify,
    completeAuth: fake.complete,
    getAuthMe: fake.me,
    authLogout: fake.logout,
    listAuthDevices: fake.listDevices,
    revokeAuthDevice: fake.revokeDevice,
    bindAuthIdentity: fake.bind,
  }
})

// 必须在 vi.mock 之后 import：组件要拿到被替换过的 api 模块
const { default: HubLoginPanel } = await import('./HubLoginPanel')

/** 清掉上一用例的登录态（假后端的状态是模块级的）。 */
beforeEach(() => {
  resetFake()
})

afterEach(cleanup)

const openPanel = () => render(<HubLoginPanel show onClose={() => {}} />)

/** 等 A1 出现（首次挂载会先探 /auth/status，有 180ms 延迟）。 */
const waitForA1 = () => screen.findByText('欢迎使用 HaiTun Agent')

const typePhone = (v: string) => {
  fireEvent.change(screen.getByLabelText('手机号'), { target: { value: v } })
}

const checkAgree = () => fireEvent.click(screen.getByLabelText('同意协议'))

/** 把 6 位码整段粘进第 1 格。 */
const fillCode = (code: string) => {
  fireEvent.change(screen.getByLabelText('验证码第 1 位'), { target: { value: code } })
}

describe('屏 A1：输入手机号', () => {
  it('渲染品牌头、双 Tab 与协议勾选', async () => {
    openPanel()
    await waitForA1()
    expect(screen.getByRole('tab', { name: '手机号' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: '邮箱' })).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByText(/我已阅读并同意/)).toBeTruthy()
  })

  it('号码格式非法时主按钮禁用，合法后亮起', async () => {
    openPanel()
    await waitForA1()
    const btn = screen.getByRole('button', { name: '获取验证码' })
    typePhone('138')
    expect(btn).toBeDisabled()
    typePhone('13800138000')
    expect(btn).not.toBeDisabled()
  })

  it('未勾协议就点：不进下一屏，勾选框抖动', async () => {
    openPanel()
    await waitForA1()
    typePhone('13800138001')
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    // 仍停在 A1，没有跳到验证码屏
    expect(screen.queryByLabelText('验证码第 1 位')).toBeNull()
    expect(document.querySelector('.hub-agree .box.shake')).toBeTruthy()
  })
})

describe('屏 B1：Tab 切到邮箱', () => {
  it('切 Tab 后换成邮箱输入框，文案随之改', async () => {
    openPanel()
    await waitForA1()
    fireEvent.click(screen.getByRole('tab', { name: '邮箱' }))
    expect(screen.getByLabelText('邮箱')).toBeTruthy()
    expect(screen.queryByLabelText('手机号')).toBeNull()
    expect(screen.getByText(/验证邮箱即可登录/)).toBeTruthy()
  })

  it('两侧输入互不清空（原型 B1 硬要求）', async () => {
    openPanel()
    await waitForA1()
    typePhone('13800138000')
    fireEvent.click(screen.getByRole('tab', { name: '邮箱' }))
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'a@b.com' } })
    fireEvent.click(screen.getByRole('tab', { name: '手机号' }))
    // 回到手机号那条，原先填的号还在
    expect(screen.getByLabelText('手机号')).toHaveValue('138 0013 8000')
    fireEvent.click(screen.getByRole('tab', { name: '邮箱' }))
    expect(screen.getByLabelText('邮箱')).toHaveValue('a@b.com')
  })
})

describe('屏 A2：验证码', () => {
  it('发码后进 A2，号码中间四位打码，有效期 5 分钟', async () => {
    openPanel()
    await waitForA1()
    typePhone(SCENARIOS.existing)
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    expect(await screen.findByLabelText('验证码第 1 位')).toBeTruthy()
    expect(screen.getByText('+86 138****8000')).toBeTruthy()
    expect(screen.getByText(/请在 5 分钟内完成验证/)).toBeTruthy()
  })

  it('整段粘贴自动分填 6 格', async () => {
    openPanel()
    await waitForA1()
    typePhone(SCENARIOS.existing)
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    await screen.findByLabelText('验证码第 1 位')
    fillCode('987654')
    for (let i = 0; i < 6; i += 1) {
      expect(screen.getByLabelText(`验证码第 ${i + 1} 位`)).toHaveValue('987654'[i])
    }
  })

  it('返回箭头在标题栏，回 A1 且保留已填号码', async () => {
    openPanel()
    await waitForA1()
    typePhone(SCENARIOS.existing)
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    await screen.findByLabelText('验证码第 1 位')
    fireEvent.click(screen.getByLabelText('返回'))
    await waitForA1()
    expect(screen.getByLabelText('手机号')).toHaveValue('138 0013 8000')
  })
})

describe('屏 B2：邮箱验证码', () => {
  it('邮箱完整显示不打码，有效期 10 分钟，垃圾邮件提示不可点', async () => {
    openPanel()
    await waitForA1()
    fireEvent.click(screen.getByRole('tab', { name: '邮箱' }))
    fireEvent.change(screen.getByLabelText('邮箱'), {
      target: { value: SCENARIOS.existingEmail },
    })
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    await screen.findByLabelText('验证码第 1 位')
    expect(screen.getByText(SCENARIOS.existingEmail)).toBeTruthy()
    expect(screen.getByText(/请在 10 分钟内完成验证/)).toBeTruthy()
    // 纯文案：不能是 button
    const hint = screen.getByText('没收到？请检查垃圾邮件文件夹')
    expect(hint.tagName).not.toBe('BUTTON')
  })
})

describe('屏 A3 → D4 → C1：新用户建号后到账户面板', () => {
  it('新号验证通过进 A3，提交后落到 C1 已登录', async () => {
    openPanel()
    await waitForA1()
    typePhone('13900001111') // 非 SCENARIOS.existing，走新用户
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    await screen.findByLabelText('验证码第 1 位')
    fillCode(MOCK_CODE) // 填满即自动提交
    // A3
    expect(await screen.findByText(/为您创建新账号/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('昵称'), { target: { value: '测试用户' } })
    fireEvent.click(screen.getByRole('button', { name: '开始使用' }))
    // D4：建号收尾期间不可中断
    expect(await screen.findByText('正在为您准备账号…')).toBeTruthy()
    expect(screen.queryByLabelText('关闭')).toBeNull()
    /* C1。timeout 放宽到 4s：这一步串了 complete(700ms) + status(180ms) +
       me(150ms) 三个 mock 延迟，默认 1000ms 不够 —— 是测试等得太短，
       不是界面慢。 */
    expect(await screen.findByText('已登录', {}, { timeout: 4000 })).toBeTruthy()
    expect(screen.getByText('测试用户')).toBeTruthy()
    expect(screen.getByText(/退出登录不会删除任何本地数据/)).toBeTruthy()
  })
})

describe('屏 C1 → C2：设备管理', () => {
  const loginAsExisting = async () => {
    openPanel()
    await waitForA1()
    typePhone(SCENARIOS.existing)
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    await screen.findByLabelText('验证码第 1 位')
    fillCode(MOCK_CODE)
    await screen.findByText('已登录')
  }

  it('老用户直接登录，跳过 A3', async () => {
    await loginAsExisting()
    expect(screen.getByText('海豚用户 8000')).toBeTruthy()
  })

  it('进 C2 列出 3 台设备，本机行无移除按钮', async () => {
    await loginAsExisting()
    fireEvent.click(screen.getByRole('button', { name: /管理登录设备/ }))
    expect(await screen.findByText(/移除设备后/)).toBeTruthy()
    expect(screen.getByText('本机')).toBeTruthy()
    // 3 台设备里只有 2 台可移除
    expect(screen.getAllByRole('button', { name: '移除设备' })).toHaveLength(2)
  })

  it('移除一台后列表少一行', async () => {
    await loginAsExisting()
    fireEvent.click(screen.getByRole('button', { name: /管理登录设备/ }))
    await screen.findByText(/移除设备后/)
    fireEvent.click(screen.getAllByRole('button', { name: '移除设备' })[0])
    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: '移除设备' })).toHaveLength(1)
    })
  })
})

describe('屏 D1：验证码错误', () => {
  it('码错时 6 格整体转红并显示剩余次数', async () => {
    openPanel()
    await waitForA1()
    typePhone(SCENARIOS.existing)
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    await screen.findByLabelText('验证码第 1 位')
    fillCode('000000')
    expect(await screen.findByText('验证码不正确，还可尝试 4 次')).toBeTruthy()
    expect(document.querySelector('.hub-otp.bad')).toBeTruthy()
  })
})

describe('屏 D2：发码被限频', () => {
  it('429 时显示黄条与按钮内倒计时，秒数取服务端 retryAfter', async () => {
    openPanel()
    await waitForA1()
    typePhone(SCENARIOS.rateLimited)
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    expect(await screen.findByText(/发送过于频繁/)).toBeTruthy()
    // 48 来自 mock 的 retryAfter，不是前端拍的 60
    expect(screen.getByRole('button', { name: /重新获取（48s）/ })).toBeDisabled()
  })

  it('当日上限换成另一套文案，指向邮箱兜底', async () => {
    openPanel()
    await waitForA1()
    typePhone(SCENARIOS.dailyCap)
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    expect(await screen.findByText(/今日发送次数已达上限/)).toBeTruthy()
  })
})

describe('屏 D3：无法连接', () => {
  it('上游不可达时转 D3，给重试与「暂不登录」两个出口', async () => {
    openPanel()
    await waitForA1()
    typePhone(SCENARIOS.offline)
    checkAgree()
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    expect(await screen.findByText('暂时无法连接')).toBeTruthy()
    expect(screen.getByText('登录需要联网，本机功能不受影响')).toBeTruthy()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '暂不登录，继续使用' })).toBeTruthy()
  })

  // 回归：首次探测就失败时不能永久转圈。
  //
  // refresh() 的 catch 只 setFail('D3')、**不设 status**，所以 status 仍是 null。
  // body 选择若把 `status === null` 判在 `fail === 'D3'` 之前，界面就永远停在
  // 「正在检查登录状态…」——转圈转到底，renderOffline() 里的「重试」和
  // 「暂不登录，继续使用」两个出口一个都点不到，用户既看不到原因也退不出去。
  // 上面那条用例走的是**发码**失败，进 D3 时 status 已经探到了，遮不住这个次序问题。
  it('初次探测 /auth/status 就抛错时，显示错误屏而不是一直 loading', async () => {
    const api = await import('../../services/api')
    const spy = vi.spyOn(api, 'getAuthStatus').mockRejectedValue(new Error('network down'))
    try {
      openPanel()
      // 必须出现出口按钮；出现「正在检查登录状态…」则说明卡在 loading
      expect(await screen.findByRole('button', { name: '重试' })).toBeTruthy()
      expect(screen.getByRole('button', { name: '暂不登录，继续使用' })).toBeTruthy()
      expect(screen.queryByText('正在检查登录状态…')).toBeNull()
    } finally {
      spy.mockRestore()
    }
  })
})
