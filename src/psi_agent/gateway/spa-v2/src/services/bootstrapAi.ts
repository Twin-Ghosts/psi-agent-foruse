import { createAi, deleteAi, listAis, type AiInfo } from './api'

/**
 * Remote free-model endpoint (company domain). Real upstream key lives only on
 * the VM behind this proxy; SPA ships a placeholder Bearer.
 *
 * Do NOT POST this on boot when the pool is empty and there are no Sessions —
 * open the models panel first. When Sessions already bind dangling ``ai_id``s,
 * ``hydrateAiForSessions`` / ``reviveMissingSessionAis`` recreate free AIs under
 * those ids so refresh does not break chat.
 */
/** Aligns with Hub model pool DeepSeek preset (`deepseek-v4-flash`); key injected on VPS. */
export const DEFAULT_REMOTE_AI = {
  provider: 'openai',
  model: 'deepseek-v4-flash',
  base_url: 'https://misakamikoto.genuineknowledge.cn',
  api_key: 'haitun-default',
}

export const PLACEHOLDER_API_KEY = 'haitun-default'

const LS_SELECTED_AI = 'spa-v2-selected-ai'

/** Config fingerprint — same provider/model/key/base ⇒ one row in the Hub list. */
export function aiConfigKey(
  ai: Pick<AiInfo, 'provider' | 'model' | 'api_key' | 'base_url'>,
): string {
  const base = (ai.base_url ?? '').trim().replace(/\/+$/, '')
  return [ai.provider ?? '', ai.model ?? '', ai.api_key ?? '', base].join('\0')
}

/**
 * Collapse AIs that differ only by instance id (e.g. free-path revive under
 * multiple Session ``ai_id``s). Different ``api_key`` (or model/base) stay separate.
 * When ``preferredId`` is in a duplicate group, that instance is the survivor.
 */
export function dedupeAisForDisplay(
  ais: AiInfo[],
  preferredId?: string | null,
): AiInfo[] {
  if (!Array.isArray(ais) || ais.length === 0) return []
  const prefer = preferredId?.trim() || ''
  const byKey = new Map<string, AiInfo>()
  for (const a of ais) {
    const key = aiConfigKey(a)
    const prev = byKey.get(key)
    if (!prev) {
      byKey.set(key, a)
      continue
    }
    if (prefer && a.id === prefer) byKey.set(key, a)
  }
  return [...byKey.values()]
}

/** True for free-path / broken placeholder entries (must not win over real keys). */
export function isPlaceholderAi(ai: Pick<AiInfo, 'api_key'> | null | undefined): boolean {
  const key = (ai?.api_key ?? '').trim()
  return !key || key === PLACEHOLDER_API_KEY
}

export function readStoredAiId(): string | null {
  try {
    const raw = localStorage.getItem(LS_SELECTED_AI)
    return raw?.trim() || null
  } catch {
    return null
  }
}

export function writeStoredAiId(id: string | null): void {
  try {
    if (id?.trim()) localStorage.setItem(LS_SELECTED_AI, id.trim())
    else localStorage.removeItem(LS_SELECTED_AI)
  } catch {
    // ignore quota / private mode
  }
}

/**
 * Prefer: explicit id (if still present) → stored id → first real key → first entry.
 * Never prefer a placeholder when any real AI exists.
 */
export function pickPreferredAi(
  ais: AiInfo[],
  preferredId?: string | null,
): AiInfo | null {
  if (!Array.isArray(ais) || ais.length === 0) return null
  const real = ais.filter((a) => !isPlaceholderAi(a))
  const pool = real.length > 0 ? real : ais

  const want = preferredId?.trim()
  if (want) {
    const hit = pool.find((a) => a.id === want)
    if (hit) return hit
    // Preferred was a placeholder while real AIs exist — fall through.
    const anyHit = ais.find((a) => a.id === want)
    if (anyHit && real.length === 0) return anyHit
  }

  const stored = readStoredAiId()
  if (stored) {
    const hit = pool.find((a) => a.id === stored)
    if (hit) return hit
  }

  return pool[0] ?? null
}

/** Wipe the local AI pool (user config). Empty pool = free/remote path. */
export async function clearAiPool(): Promise<void> {
  const existing = await listAis()
  if (!Array.isArray(existing) || existing.length === 0) return
  await Promise.all(existing.map((a) => deleteAi(a.id)))
  writeStoredAiId(null)
}

/**
 * Remove placeholder free AIs when the user already has a real key configured.
 * Prevents boot/`ais[0]` from binding new sessions to `haitun-default`.
 */
export async function purgePlaceholderAis(): Promise<AiInfo[]> {
  const existing = await listAis()
  if (!Array.isArray(existing) || existing.length === 0) return []
  const placeholders = existing.filter((a) => isPlaceholderAi(a))
  const real = existing.filter((a) => !isPlaceholderAi(a))
  if (placeholders.length === 0 || real.length === 0) return existing
  await Promise.all(placeholders.map((a) => deleteAi(a.id)))
  return listAis()
}

/**
 * Resolve an AI for chat/session when the pool is empty: create the remote
 * free default. If AIs already exist, return the preferred real one.
 * Call only at use time (new task / new session), never on SPA boot alone.
 */
export async function ensureDefaultAi(
  preferredId?: string | null,
): Promise<AiInfo | null> {
  try {
    const existing = await listAis()
    if (Array.isArray(existing) && existing.length > 0) {
      return pickPreferredAi(existing, preferredId)
    }
    const info = await createAi({ ...DEFAULT_REMOTE_AI })
    if (info?.id) {
      writeStoredAiId(info.id)
      return info
    }
  } catch {
    // Proxy unreachable or create failed — Hub models panel can still configure.
  }
  try {
    const again = await listAis()
    return pickPreferredAi(again, preferredId)
  } catch {
    return null
  }
}

/**
 * Recreate free remote AIs for Session ``ai_id``s missing from the pool.
 *
 * Gateway does not cascade-delete Sessions when AIs are wiped; refresh / 「使用
 * 免费模型」 leave dangling backends. Same-id revive keeps history + titles.
 */
export async function reviveMissingSessionAis(
  sessionAiIds: Iterable<string | null | undefined>,
): Promise<AiInfo[]> {
  const want = [
    ...new Set(
      [...sessionAiIds]
        .map((id) => (typeof id === 'string' ? id.trim() : ''))
        .filter((id) => id.length > 0),
    ),
  ]
  let existing: AiInfo[] = []
  try {
    existing = await listAis()
  } catch {
    return []
  }
  if (!Array.isArray(existing)) existing = []
  const have = new Set(existing.map((a) => a.id))
  for (const id of want) {
    if (have.has(id)) continue
    try {
      const revived = await createAi({ ...DEFAULT_REMOTE_AI, id })
      if (revived?.id) {
        have.add(revived.id)
        existing = [...existing, revived]
      }
    } catch {
      // Race: already exists — refresh membership from server.
      try {
        existing = await listAis()
        for (const a of existing) have.add(a.id)
      } catch {
        /* keep going */
      }
    }
  }
  try {
    return await listAis()
  } catch {
    return existing
  }
}

/**
 * Single workbench AI hydrate (boot + Hub free-switch share this).
 *
 * 1. Drop leftover free placeholders when a real key exists
 * 2. Revive dangling Session backends as free remotes (same id)
 * 3. Pick UI selection; only open models when the pool is still empty
 */
export async function hydrateAiForSessions(
  sessionAiIds: Iterable<string | null | undefined>,
  preferredId?: string | null,
): Promise<{ ais: AiInfo[]; preferred: AiInfo | null; openModels: boolean }> {
  await purgePlaceholderAis()
  const ais = await reviveMissingSessionAis(sessionAiIds)
  const preferred = pickPreferredAi(ais, preferredId)
  if (preferred?.id) writeStoredAiId(preferred.id)
  return {
    ais,
    preferred,
    openModels: ais.length === 0,
  }
}

/**
 * Keep an existing Session's backend alive after 「使用免费模型」 wiped the pool.
 *
 * Sessions keep their ``ai_id``; ``clearAiPool`` deletes the AI entry, so chat
 * would hit a dead socket. Recreate the free remote AI under the **same id**
 * (no Session delete → history/titles stay). If there is no prior id, fall back
 * to ``ensureDefaultAi``.
 */
export async function ensureSessionAi(
  sessionAiId?: string | null,
): Promise<AiInfo | null> {
  const want = sessionAiId?.trim() || null
  if (want) {
    const revived = await reviveMissingSessionAis([want])
    const hit = revived.find((a) => a.id === want)
    if (hit) {
      writeStoredAiId(hit.id)
      return hit
    }
  }
  return ensureDefaultAi(want)
}
