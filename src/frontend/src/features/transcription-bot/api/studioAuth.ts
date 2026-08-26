import { useCallback, useRef } from 'react'
import { fetchApi } from '@/api/fetchApi'
import { useConfig } from '@/api/useConfig'
import type { LintoRuntimeConfig } from '@/api/useConfig'
import LinTO from '../vendor/linto-sdk'

/**
 * Browser-first LinTO Studio authentication.
 *
 * The panel talks to studio-api DIRECTLY via the JS SDK, authenticated as the
 * REAL user. Two ways to obtain the user's Studio JWT:
 *  - the shared-IdP SILENT SSO (`sso_enabled`) — the browser already holds the
 *    LemonLDAP/Keycloak session from the Meet login, so a `prompt=none` OIDC
 *    round-trip to studio-api returns a Studio JWT with no re-typing;
 *  - a DEV bridge (`dev_token_enabled`) — the Meet backend mints a Studio JWT
 *    from the service account, so the flow is testable before the shared-IdP
 *    client is registered (lot 2).
 */

export class StudioAuthUnavailable extends Error {
  constructor(message = 'LinTO Studio authentication is not configured') {
    super(message)
    this.name = 'StudioAuthUnavailable'
  }
}

export interface StudioClient {
  linto: InstanceType<typeof LinTO>
  organizationId: string
}

const trimSlash = (s: string) => s.replace(/\/+$/, '')

interface StudioToken {
  token: string
  baseUrl: string
}

/**
 * Silent SSO against studio-api. First tries the token endpoint with the
 * ambient Studio session cookie; if none exists, drives a hidden-iframe OIDC
 * login (`prompt=none`) at the shared IdP — the browser already carries that
 * session from the Meet login — then retries the token endpoint.
 *
 * NOTE: end-to-end this can only be validated once the shared-IdP client for
 * Studio is registered (lot 2). The mechanics (endpoints, credentials, hidden
 * iframe, polling) are in place and configuration-driven.
 */
async function silentStudioLogin(
  studioApiUrl: string,
  loginPath: string,
  tokenPath: string
): Promise<string> {
  const base = trimSlash(studioApiUrl)
  const tokenUrl = `${base}${tokenPath}`
  const loginUrl = `${base}${loginPath}?prompt=none`

  const readToken = async (): Promise<string | null> => {
    try {
      const res = await fetch(tokenUrl, { credentials: 'include' })
      if (!res.ok) return null
      const body = (await res.json()) as { authToken?: string; token?: string }
      return body.authToken || body.token || null
    } catch {
      return null
    }
  }

  // 1. A live Studio session cookie returns the JWT straight away.
  const existing = await readToken()
  if (existing) return existing

  // 2. Otherwise, drive a silent (prompt=none) OIDC login in a hidden iframe and
  //    poll the token endpoint until the callback has set the Studio cookie.
  await new Promise<void>((resolve) => {
    if (typeof document === 'undefined') {
      resolve()
      return
    }
    const iframe = document.createElement('iframe')
    iframe.style.display = 'none'
    iframe.src = loginUrl
    let settled = false
    const done = () => {
      if (settled) return
      settled = true
      try {
        document.body.removeChild(iframe)
      } catch {
        /* already gone */
      }
      resolve()
    }
    iframe.addEventListener('load', () => {
      // The callback redirects the iframe back to a Studio page; give the cookie
      // a beat to land, then resolve. A cross-origin frame throws on access —
      // that's expected and harmless.
      setTimeout(done, 500)
    })
    // Hard timeout so a missing/blocked session never hangs the panel.
    setTimeout(done, 8000)
    document.body.appendChild(iframe)
  })

  const token = await readToken()
  if (!token) {
    throw new StudioAuthUnavailable(
      'silent SSO did not yield a Studio token (no shared session?)'
    )
  }
  return token
}

/** DEV bridge: the Meet backend mints a Studio JWT from the service account. */
async function devStudioToken(
  roomId: string,
  roomToken: string
): Promise<StudioToken> {
  const search = roomToken ? `?token=${encodeURIComponent(roomToken)}` : ''
  const res = await fetchApi<{ token: string; base_url: string }>(
    `rooms/${roomId}/linto/studio-token/${search}`
  )
  return { token: res.token, baseUrl: res.base_url }
}

async function resolveOrganizationId(
  linto: InstanceType<typeof LinTO>,
  config: LintoRuntimeConfig
): Promise<string> {
  if (config.default_org_id) return config.default_org_id
  const orgs = await linto.apiService.fetchOrganizations()
  if (!orgs || orgs.length === 0) {
    throw new StudioAuthUnavailable('the user has no LinTO organization')
  }
  const visio = orgs.find((o) => (o.name || '').toLowerCase().includes('visio'))
  return (visio ?? orgs[0])._id
}

/**
 * Build an authenticated LinTO SDK client for the current user + resolve the
 * organization to act in. Throws {@link StudioAuthUnavailable} when neither the
 * SSO nor the dev bridge is configured.
 */
export async function getStudioClient(
  roomId: string,
  roomToken: string,
  config: LintoRuntimeConfig
): Promise<StudioClient> {
  let authToken: string
  let baseUrl: string
  if (config.sso_enabled) {
    baseUrl = config.studio_api_url
    authToken = await silentStudioLogin(
      config.studio_api_url,
      config.sso_login_path,
      config.sso_token_path
    )
  } else if (config.dev_token_enabled) {
    const dev = await devStudioToken(roomId, roomToken)
    authToken = dev.token
    baseUrl = dev.baseUrl || config.studio_api_url
  } else {
    throw new StudioAuthUnavailable()
  }

  const linto = new LinTO({ authToken, baseUrl })
  const organizationId = await resolveOrganizationId(linto, config)
  return { linto, organizationId }
}

/**
 * React hook: lazily builds the Studio client on first use and memoizes it for
 * the room+config lifetime, so start/stop/profiles reuse one authenticated
 * client instead of re-authenticating each call.
 */
export const useStudioClient = () => {
  const { data: apiConfig } = useConfig()
  const linto = apiConfig?.linto
  const cacheRef = useRef<{
    key: string
    promise: Promise<StudioClient>
  } | null>(null)

  const getClient = useCallback(
    (roomId: string, roomToken: string): Promise<StudioClient> => {
      if (!linto) return Promise.reject(new StudioAuthUnavailable())
      const key = `${roomId}:${linto.studio_api_url}:${linto.sso_enabled}:${linto.dev_token_enabled}`
      if (cacheRef.current?.key !== key) {
        cacheRef.current = {
          key,
          promise: getStudioClient(roomId, roomToken, linto).catch((err) => {
            // Don't cache a failed auth — allow a retry on the next Start.
            if (cacheRef.current?.key === key) cacheRef.current = null
            throw err
          }),
        }
      }
      return cacheRef.current.promise
    },
    [linto]
  )

  return { getClient, config: linto }
}
