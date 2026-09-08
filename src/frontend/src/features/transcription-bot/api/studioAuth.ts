import { useCallback, useRef } from 'react'
import { fetchApi } from '@/api/fetchApi'
import { ApiError } from '@/api/ApiError'
import { useConfig } from '@/api/useConfig'
import type { LintoRuntimeConfig } from '@/api/useConfig'
import LinTO from '../vendor/linto-sdk'

/**
 * Browser-first LinTO Studio authentication.
 *
 * The panel talks to studio-api DIRECTLY via the JS SDK. The Studio JWT comes
 * from ONE place: the Meet backend (`GET rooms/{id}/linto/studio-token/`),
 * which knows the participant from the LiveKit room token and hands back a
 * token from the configured source (the shared service account, or the user's
 * own LinTO API key through the studio-api identity exchange) together with
 * the organization to act in. The browser never holds a long-lived key: the
 * token carries an `expires_in` hint and the client is rebuilt before expiry.
 */

export class StudioAuthUnavailable extends Error {
  constructor(message = 'LinTO Studio authentication is not configured') {
    super(message)
    this.name = 'StudioAuthUnavailable'
  }
}

/**
 * The LinTO option is not active for this account: no LinTO key is linked to
 * the user's identity (`no_entitlement`), or the linked key may not run a
 * quickMeeting (`quick_meeting_disabled`). Distinct from an upstream failure —
 * the panel shows a dedicated state instead of an error.
 */
export class StudioNotEntitled extends Error {
  reason: string
  constructor(reason = 'no_entitlement') {
    super(`the LinTO option is not active for this account (${reason})`)
    this.name = 'StudioNotEntitled'
    this.reason = reason
  }
}

export interface StudioCapabilities {
  quickMeeting: boolean
}

export interface StudioClient {
  linto: InstanceType<typeof LinTO>
  organizationId: string
  capabilities: StudioCapabilities
  // Epoch ms at which the Studio token expires; null = unknown / long-lived.
  expiresAt: number | null
}

// Wire shape of GET rooms/{id}/linto/studio-token/.
interface StudioTokenResponse {
  enabled: boolean
  reason?: string
  token?: string
  base_url?: string
  organization_id?: string
  expires_in?: number | null
  capabilities?: Partial<StudioCapabilities>
}

// Rebuild the client this long before the token expires (a long meeting must
// not lose Stop / profile calls to an expired token).
const REFRESH_MARGIN_MS = 60_000

/**
 * The Meet backend is the identity bridge: it returns the Studio JWT + context
 * the SDK acts with for the current participant. Anonymous guests have no Meet
 * identity to bridge (403) — they can still read a running transcript through
 * the public routes, so that maps to {@link StudioAuthUnavailable}.
 */
async function fetchStudioToken(
  roomId: string,
  roomToken: string
): Promise<StudioTokenResponse> {
  const search = roomToken ? `?token=${encodeURIComponent(roomToken)}` : ''
  try {
    return await fetchApi<StudioTokenResponse>(
      `rooms/${roomId}/linto/studio-token/${search}`
    )
  } catch (err) {
    if (err instanceof ApiError && err.statusCode === 403) {
      throw new StudioAuthUnavailable(
        'anonymous participants have no LinTO identity'
      )
    }
    throw err
  }
}

/**
 * Build an authenticated LinTO SDK client for the current participant. The
 * organization comes from the token response (it is the key's organization),
 * never from a name heuristic. Throws {@link StudioNotEntitled} when the option
 * is not active for this account, {@link StudioAuthUnavailable} otherwise.
 */
export async function getStudioClient(
  roomId: string,
  roomToken: string,
  config: LintoRuntimeConfig
): Promise<StudioClient> {
  const res = await fetchStudioToken(roomId, roomToken)
  if (!res.enabled || !res.token) {
    throw new StudioNotEntitled(res.reason || 'no_entitlement')
  }
  const capabilities: StudioCapabilities = {
    quickMeeting: res.capabilities?.quickMeeting !== false,
  }
  // `metadata.quickMeeting=false` on the key is honoured on BOTH sides: Studio
  // refuses the quickMeeting, and the panel never offers Start.
  if (!capabilities.quickMeeting) {
    throw new StudioNotEntitled('quick_meeting_disabled')
  }
  if (!res.organization_id) {
    throw new StudioAuthUnavailable(
      'the Studio token has no organization to act in'
    )
  }
  const linto = new LinTO({
    authToken: res.token,
    baseUrl: res.base_url || config.studio_api_url,
  })
  const expiresAt =
    typeof res.expires_in === 'number' && res.expires_in > 0
      ? Date.now() + res.expires_in * 1000
      : null
  return {
    linto,
    organizationId: res.organization_id,
    capabilities,
    expiresAt,
  }
}

interface CacheEntry {
  key: string
  promise: Promise<StudioClient>
  expiresAt: number | null
}

/**
 * React hook: lazily builds the Studio client on first use and memoizes it for
 * the room+config lifetime, so start/stop/profiles reuse one authenticated
 * client instead of re-authenticating each call. The entry is dropped when the
 * token nears expiry (the next call re-fetches) and on failure (retry on the
 * next Start).
 */
export const useStudioClient = () => {
  const { data: apiConfig } = useConfig()
  const linto = apiConfig?.linto
  const cacheRef = useRef<CacheEntry | null>(null)

  const getClient = useCallback(
    (roomId: string, roomToken: string): Promise<StudioClient> => {
      if (!linto) return Promise.reject(new StudioAuthUnavailable())
      const key = `${roomId}:${linto.studio_api_url}:${linto.token_source}`
      const cached = cacheRef.current
      const expiring =
        cached?.expiresAt != null &&
        Date.now() > cached.expiresAt - REFRESH_MARGIN_MS
      if (cached && cached.key === key && !expiring) return cached.promise
      const entry = { key, expiresAt: null } as CacheEntry
      entry.promise = getStudioClient(roomId, roomToken, linto).then(
        (client) => {
          entry.expiresAt = client.expiresAt
          return client
        },
        (err) => {
          // Don't cache a failed auth — allow a retry on the next call.
          if (cacheRef.current === entry) cacheRef.current = null
          throw err
        }
      )
      cacheRef.current = entry
      return entry.promise
    },
    [linto]
  )

  return { getClient, config: linto }
}
