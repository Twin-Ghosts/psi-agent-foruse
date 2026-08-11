import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AiInfo } from './api'
import {
  dedupeAisForDisplay,
  hydrateAiForSessions,
  isPlaceholderAi,
  pickPreferredAi,
  PLACEHOLDER_API_KEY,
  reviveMissingSessionAis,
} from './bootstrapAi'

vi.mock('./api', () => ({
  listAis: vi.fn(),
  createAi: vi.fn(),
  deleteAi: vi.fn(),
}))

import { createAi, deleteAi, listAis } from './api'

const ai = (partial: Partial<AiInfo> & Pick<AiInfo, 'id' | 'api_key'>): AiInfo => ({
  id: partial.id,
  socket: partial.socket ?? '',
  provider: partial.provider ?? 'deepseek',
  model: partial.model ?? 'deepseek-v4-flash',
  api_key: partial.api_key,
  base_url: partial.base_url ?? 'https://api.deepseek.com/v1',
})

describe('dedupeAisForDisplay', () => {
  it('collapses same config different ids; keeps preferred', () => {
    const a = ai({
      id: 'a',
      api_key: PLACEHOLDER_API_KEY,
      provider: 'openai',
      model: 'deepseek-v4-flash',
      base_url: 'https://misakamikoto.genuineknowledge.cn/',
    })
    const b = ai({
      id: 'b',
      api_key: PLACEHOLDER_API_KEY,
      provider: 'openai',
      model: 'deepseek-v4-flash',
      base_url: 'https://misakamikoto.genuineknowledge.cn',
    })
    expect(dedupeAisForDisplay([a, b]).map((x) => x.id)).toEqual(['a'])
    expect(dedupeAisForDisplay([a, b], 'b').map((x) => x.id)).toEqual(['b'])
  })

  it('keeps rows that differ by api_key', () => {
    const free = ai({ id: 'free', api_key: PLACEHOLDER_API_KEY, provider: 'openai' })
    const real = ai({ id: 'real', api_key: 'sk-real', provider: 'openai' })
    expect(dedupeAisForDisplay([free, real]).map((x) => x.id).sort()).toEqual(['free', 'real'])
  })
})

describe('isPlaceholderAi', () => {
  it('detects haitun-default and empty keys', () => {
    expect(isPlaceholderAi(ai({ id: '1', api_key: PLACEHOLDER_API_KEY }))).toBe(true)
    expect(isPlaceholderAi(ai({ id: '2', api_key: '' }))).toBe(true)
    expect(isPlaceholderAi(ai({ id: '3', api_key: 'sk-real' }))).toBe(false)
  })
})

describe('pickPreferredAi', () => {
  const free = ai({ id: 'free', api_key: PLACEHOLDER_API_KEY, provider: 'openai' })
  const realA = ai({ id: 'real-a', api_key: 'sk-a' })
  const realB = ai({ id: 'real-b', api_key: 'sk-b' })

  it('skips placeholder when real AIs exist', () => {
    expect(pickPreferredAi([free, realA, realB])?.id).toBe('real-a')
  })

  it('honors preferred real id', () => {
    expect(pickPreferredAi([free, realA, realB], 'real-b')?.id).toBe('real-b')
  })

  it('ignores preferred placeholder when real AIs exist', () => {
    expect(pickPreferredAi([free, realA], 'free')?.id).toBe('real-a')
  })

  it('falls back to placeholder only when pool is free-only', () => {
    expect(pickPreferredAi([free])?.id).toBe('free')
  })
})

describe('reviveMissingSessionAis', () => {
  beforeEach(() => {
    vi.mocked(listAis).mockReset()
    vi.mocked(createAi).mockReset()
    vi.mocked(deleteAi).mockReset()
  })

  it('recreates free AI under dangling session ids', async () => {
    const revived = ai({ id: 'sess-ai', api_key: PLACEHOLDER_API_KEY, provider: 'openai' })
    vi.mocked(listAis)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([revived])
    vi.mocked(createAi).mockResolvedValue(revived)

    const out = await reviveMissingSessionAis(['sess-ai', 'sess-ai', '  '])
    expect(createAi).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'sess-ai', api_key: PLACEHOLDER_API_KEY }),
    )
    expect(out.map((a) => a.id)).toEqual(['sess-ai'])
  })

  it('skips ids already in the pool', async () => {
    const existing = ai({ id: 'alive', api_key: 'sk-x' })
    vi.mocked(listAis).mockResolvedValue([existing])
    const out = await reviveMissingSessionAis(['alive'])
    expect(createAi).not.toHaveBeenCalled()
    expect(out).toEqual([existing])
  })
})

describe('hydrateAiForSessions', () => {
  beforeEach(() => {
    vi.mocked(listAis).mockReset()
    vi.mocked(createAi).mockReset()
    vi.mocked(deleteAi).mockReset()
  })

  it('opens models only when pool stays empty', async () => {
    vi.mocked(listAis).mockResolvedValue([])
    const empty = await hydrateAiForSessions([])
    expect(empty.openModels).toBe(true)
    expect(empty.preferred).toBeNull()
  })

  it('revives session backends and does not open models', async () => {
    const revived = ai({ id: 'old', api_key: PLACEHOLDER_API_KEY, provider: 'openai' })
    vi.mocked(listAis)
      .mockResolvedValueOnce([]) // purge
      .mockResolvedValueOnce([]) // revive start
      .mockResolvedValueOnce([revived]) // revive end
    vi.mocked(createAi).mockResolvedValue(revived)

    const out = await hydrateAiForSessions(['old'])
    expect(out.openModels).toBe(false)
    expect(out.preferred?.id).toBe('old')
  })
})
