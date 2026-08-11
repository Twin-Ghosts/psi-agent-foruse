import { streamChat } from './api'
import { appendChatFilesToFormData } from './chatFiles'
import { readSSE } from './sse'
import type { ChatFile } from '../haitun-agent/model'

export type StreamHandlers = {
  onText?: (delta: string) => void
  onBlob?: (name: string, data: string, path?: string) => void
  /** ``kind`` is reasoning provenance: thinking | tool_call | tool_result. */
  onReasoning?: (delta: string, kind?: string) => void
  onError?: (message: string) => void
}

/** POST multipart chat and stream assistant text/blobs into handlers. */
export async function streamSessionChat(
  sessionId: string,
  text: string,
  files: Array<File | ChatFile> = [],
  signal?: AbortSignal,
  handlers: StreamHandlers = {},
): Promise<{ text: string; blobs: ChatFile[] }> {
  const fd = new FormData()
  const chunks: { type: string; text?: string }[] = []
  if (text.trim()) chunks.push({ type: 'text', text: text.trim() })
  fd.append('chunks', JSON.stringify(chunks))
  appendChatFilesToFormData(fd, files)

  let full = ''
  const blobs: ChatFile[] = []
  const reader = await streamChat(sessionId, fd, signal)
  const cancelReader = () => {
    void reader.cancel().catch(() => {})
  }
  if (signal) {
    if (signal.aborted) {
      cancelReader()
      throw new DOMException('Aborted', 'AbortError')
    }
    signal.addEventListener('abort', cancelReader, { once: true })
  }
  try {
    for await (const chunk of readSSE(reader)) {
      if (signal?.aborted) {
        throw new DOMException('Aborted', 'AbortError')
      }
      if (chunk.type === 'text' && typeof chunk.text === 'string') {
        full += chunk.text
        handlers.onText?.(chunk.text)
      } else if (chunk.type === 'blob' && typeof chunk.name === 'string') {
        const data = typeof chunk.data === 'string' ? chunk.data : ''
        const path = typeof chunk.path === 'string' ? chunk.path : undefined
        blobs.push({ name: chunk.name, data, ...(path ? { path } : {}) })
        handlers.onBlob?.(chunk.name, data, path)
      } else if (chunk.type === 'reasoning' && typeof chunk.text === 'string') {
        const kind = typeof chunk.kind === 'string' ? chunk.kind : undefined
        handlers.onReasoning?.(chunk.text, kind)
      } else if (chunk.type === 'error' && typeof chunk.error === 'string') {
        // 后端只在**单个文件读取失败**时发 error（见 _chat_manager._file_blob），
        // 不是整轮失败。这里以前 throw，于是 AI 在 [SEND:] 之后说的所有话都被丢掉
        // ——模型写了个不存在的路径（例如 Windows 上的 /tmp/xxx），用户看到的是
        // 回答凭空截断。一个交付物读不到不该毁掉整轮回答，故只上报、继续读流。
        handlers.onError?.(chunk.error)
      }
    }
    if (signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError')
    }
    return { text: full, blobs }
  } finally {
    signal?.removeEventListener('abort', cancelReader)
  }
}
