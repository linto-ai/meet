import * as interapp from 'cozy-interapp'
import type { IntentServiceAPI } from 'cozy-interapp'
import { ApiError } from '@/api/ApiError'
import { fetchApi } from '@/api/fetchApi'
import type { HostBridge, HostJoin } from './intentEvents'

/*
 * Meet as the *service* of a cozy intent: /intents?intent=<id> is framed by
 * the page that opens the intent (cozy-interapp puts the iframe there), and
 * cozy-interapp carries the exchange with the host (data in, results out). The
 * Meet session already exists when the host mounts the iframe: the page
 * never signs in. The stack token and the LiveKit token are never logged,
 * stored or put in a URL.
 */

// Without the service by then, the host does not speak this protocol
const JOIN_TIMEOUT_MS = 10_000

// The id of the intent, as the host sends it in the URL
const INTENT_ID = /^[A-Za-z0-9_-]{1,128}$/

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

/** What the page itself shows when no service exists to tell the host. */
export type IntentFailure = 'unauthenticated' | 'unreachable' | 'unsupported'

/** What the host gave: the room, its LiveKit token and the media choices. */
export type IntentJoin = HostJoin & { room: string; displayName?: string }

export type IntentHandlers = {
  onJoin: (join: IntentJoin) => void
  onLeave: () => void
  onError: (code: IntentFailure) => void
}

type StackToken = { access_token: string; platform_url: string }

// No usable session: a 401, or the 409 of a session from before the
// `workplaceFqdn` scope (it has no twake_instance)
const isUnauthenticated = (error: unknown) =>
  error instanceof ApiError &&
  (error.statusCode === 401 ||
    (error.statusCode === 409 &&
      typeof error.body === 'object' &&
      error.body !== null &&
      'error' in error.body &&
      error.body.error === 'no_twake_instance'))

const parseData = (data: unknown): IntentJoin | null => {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    return null
  }
  const { room, token, audio, video, displayName } = data as Record<
    string,
    unknown
  >
  if (typeof room !== 'string' || !UUID.test(room)) return null
  if (typeof token !== 'string' || !token.trim()) return null
  if (typeof audio !== 'boolean' || typeof video !== 'boolean') return null
  return {
    room,
    token,
    audio,
    video,
    ...(typeof displayName === 'string' && { displayName }),
  }
}

// The fetch of cozy-interapp: only to the platform, with the stack token
const platformFetch =
  ({ access_token, platform_url }: StackToken) =>
  async (method: string, path: string, body?: unknown) => {
    // Never leave platform_url: one leading slash, not `//host`
    if (!/^\/(?!\/)/.test(path)) throw new Error('Invalid platform path')
    const response = await fetch(`${platform_url}${path}`, {
      method,
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        Authorization: `Bearer ${access_token}`,
      },
      ...(body !== undefined && { body: JSON.stringify(body) }),
    })
    if (!response.ok) throw new Error(`Platform error ${response.status}`)
    return response.json()
  }

const isStackToken = (token: Partial<StackToken> | undefined) =>
  typeof token?.access_token === 'string' &&
  typeof token.platform_url === 'string'

type IntentsClass = typeof interapp.default
const isClass = (value: unknown): value is IntentsClass =>
  typeof value === 'function'

// ponytail: build interop of a CJS package. The production build may hand
// `default` as the exports object ({ default: Intents }), not the class.
const intentsClass = (): IntentsClass | undefined => {
  const exported: unknown = interapp.default
  if (isClass(exported)) return exported
  const inner = (exported as { default?: unknown } | null)?.default
  return isClass(inner) ? inner : undefined
}

// One service per page: a second start (React StrictMode runs an Effect twice)
// ends the first one
let active: HostBridge | null = null

/**
 * Serves the intent of the URL: gets a stack token from the Meet API, opens
 * the service with cozy-interapp, hands the page the data of the host
 * (`room`, `token`, `audio`, `video`) once, then reports `joined`, `left` or
 * `error` back to the host as results. Returns null (and does nothing) when
 * the URL has no `intent`.
 *
 * Without a usable session (a 401, or a 409 `no_twake_instance`) it says
 * `unauthenticated` and tries nothing else: it never signs in. Errors before
 * a service exists go to `onError`: only the page can tell them.
 *
 * Used by: IntentService.
 */
export const startIntentService = (
  handlers: IntentHandlers
): HostBridge | null => {
  const intent = new URLSearchParams(window.location.search).get('intent')
  if (!intent || !INTENT_ID.test(intent)) return null
  active?.close()

  let service: IntentServiceAPI | null = null
  let isClosed = false
  let isTerminated = false
  let isTimedOut = false
  let isLeaving = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let stopListening: (() => void) | undefined

  const fail = (code: IntentFailure) => {
    clearTimeout(timer)
    if (!isClosed) handlers.onError(code)
  }

  // A result for the host; nothing once the intent is terminated
  const send = (result: object, isLast = false) => {
    if (isClosed || isTerminated || !service) return
    service.sendResult(result)
    if (!isLast) return
    isTerminated = true
    service.terminate()
  }

  const bridge: HostBridge = {
    joined: () => send({ event: 'joined' }),
    left: (reason) => send({ event: 'left', reason }, true),
    error: (code) => send({ event: 'error', code }, true),
    close: () => {
      isClosed = true
      clearTimeout(timer)
      stopListening?.()
    },
  }

  const serve = async (token: StackToken) => {
    timer = setTimeout(() => {
      isTimedOut = true
      if (service) bridge.error('unsupported')
      else fail('unsupported')
    }, JOIN_TIMEOUT_MS)
    let api: IntentServiceAPI
    try {
      const Intents = intentsClass()
      if (!Intents) throw new Error('cozy-interapp: Intents class not found')
      api = await new Intents({ fetch: platformFetch(token) }).createService(
        intent,
        window
      )
    } catch (error) {
      console.error(error)
      return fail('unsupported')
    }
    // Too late (the page said unsupported) or gone: nothing is read
    if (isClosed || isTimedOut) return
    service = api
    stopListening = api.onData((message) => {
      if ((message as { leave?: unknown } | null)?.leave !== true) return
      isLeaving = true
      handlers.onLeave()
    })
    await giveData(api)
  }

  const giveData = async (api: IntentServiceAPI) => {
    let join: IntentJoin | null = null
    try {
      join = parseData(await api.getData())
    } catch (error) {
      // unreadable data is unsupported data (the error holds no secret)
      console.error(error)
    }
    if (isClosed || isTimedOut) return
    clearTimeout(timer)
    if (!join) return bridge.error('unsupported')
    // The host left while its data was awaited: no call
    if (isLeaving) return
    handlers.onJoin(join)
    api.notifyReadyToUse()
  }

  const start = async () => {
    let token: StackToken | undefined
    try {
      token = await fetchApi<StackToken>('stack-token/', { method: 'POST' })
    } catch (error) {
      if (isClosed) return
      return fail(isUnauthenticated(error) ? 'unauthenticated' : 'unreachable')
    }
    if (isClosed) return
    if (!isStackToken(token)) return fail('unreachable')
    await serve(token)
  }

  void start()
  active = bridge
  return bridge
}
