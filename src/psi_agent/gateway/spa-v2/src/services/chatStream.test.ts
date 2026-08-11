import { describe, expect, it, vi } from 'vitest'

// streamChat 是网络出口，替换成一个吐固定 SSE 的假 reader。
vi.mock('./api', () => ({
  streamChat: vi.fn(async () => makeReader(pendingSse)),
}))

import { streamSessionChat } from './chatStream'

let pendingSse = ''

/** 把一段 SSE 文本包成 ReadableStreamDefaultReader，分块吐出以模拟真实流。 */
function makeReader(sse: string): ReadableStreamDefaultReader<Uint8Array> {
  const enc = new TextEncoder()
  // 刻意切成多块：单块吐出会掩盖跨块解析的问题
  const parts = sse.match(/.{1,17}/gs) ?? []
  let i = 0
  return {
    read: async () =>
      i < parts.length ? { done: false, value: enc.encode(parts[i++]) } : { done: true, value: undefined },
    cancel: async () => {},
    releaseLock: () => {},
    closed: Promise.resolve(undefined),
  } as unknown as ReadableStreamDefaultReader<Uint8Array>
}

const ev = (o: unknown) => `data: ${JSON.stringify(o)}\n\n`

describe('streamSessionChat — 交付文件读取失败不该截断回答', () => {
  it('error 事件之后的文本仍然收得到', async () => {
    // 真实场景：模型写了个 Windows 上不存在的 /tmp 路径，后端读不到 → error 事件，
    // 但模型在标记之后还继续说了话。以前这里 throw，那些话全丢。
    pendingSse =
      ev({ type: 'text', text: '文件已生成：' }) +
      ev({ type: 'error', error: '交付文件读取失败 /tmp/delivered_file.txt: No such file' }) +
      ev({ type: 'text', text: '如需其他格式请告诉我。' })

    const seen: string[] = []
    const errors: string[] = []
    const { text } = await streamSessionChat('s1', 'hi', [], undefined, {
      onText: (d) => seen.push(d),
      onError: (m) => errors.push(m),
    })

    expect(errors).toHaveLength(1)
    expect(errors[0]).toContain('/tmp/delivered_file.txt')
    // 关键断言：error 之后的那句必须还在
    expect(text).toBe('文件已生成：如需其他格式请告诉我。')
    expect(seen).toEqual(['文件已生成：', '如需其他格式请告诉我。'])
  })

  it('一个交付物失败不影响另一个成功交付', async () => {
    pendingSse =
      ev({ type: 'error', error: '交付文件读取失败 /tmp/a.txt: No such file' }) +
      ev({ type: 'blob', name: 'b.md', data: 'YWJj', path: 'D:/ws/b.md' }) +
      ev({ type: 'text', text: '完成' })

    const blobNames: string[] = []
    const { text, blobs } = await streamSessionChat('s1', 'hi', [], undefined, {
      onBlob: (n) => blobNames.push(n),
    })

    expect(blobNames).toEqual(['b.md'])
    expect(blobs).toHaveLength(1)
    expect(blobs[0]).toMatchObject({ name: 'b.md', path: 'D:/ws/b.md' })
    expect(text).toBe('完成')
  })

  it('没有 onError 处理器时也不抛，照样读完流', async () => {
    // 实际调用方（HaiTunAgentWorkspace）当前就没传 onError，
    // 这条保证「没人接错误」不会退化成「整轮丢失」。
    pendingSse =
      ev({ type: 'error', error: '交付文件读取失败 /tmp/x: No such file' }) +
      ev({ type: 'text', text: '后续内容' })

    const { text } = await streamSessionChat('s1', 'hi', [], undefined, {})
    expect(text).toBe('后续内容')
  })
})
