import { getEnv } from '@/utils/getEnv'

/*
 * Chat page (parent) <-> Meet page /embed/<uuid> (iframe), protocol
 * twake-meet-embed v1. The token received here is never logged, stored or
 * put in a URL.
 */

const SOURCE = 'twake-meet-embed'

export type LeftReason = 'user' | 'idle' | 'removed' | 'disconnected'
export type ErrorCode =
  'unsupported' | 'token_rejected' | 'unreachable' | 'unknown'

export type HostJoin = { token: string; audio: boolean; video: boolean }

export type HostBridge = {
  joined: () => void
  left: (reason: LeftReason) => void
  error: (code: ErrorCode) => void
  close: () => void
}

type HostMessage = ({ type: 'join' } & HostJoin) | { type: 'leave' }

const allowedOrigins = () =>
  (getEnv('VITE_EMBED_ALLOWED_ORIGINS') ?? '')
    .split(',')
    .map((origin) => origin.trim())
    .filter(Boolean)

const originOf = (url: string) => {
  try {
    return new URL(url).origin
  } catch {
    return undefined
  }
}

// The parent is the first ancestor, else the referrer (Firefox has no
// ancestorOrigins)
const parentOrigin = () =>
  window.location.ancestorOrigins?.[0] ?? originOf(document.referrer)

const parseJoin = (data: Record<string, unknown>): HostMessage | null => {
  const { token, audio, video } = data
  if (typeof token !== 'string' || !token.trim()) return null
  if (typeof audio !== 'boolean' || typeof video !== 'boolean') return null
  return { type: 'join', token, audio, video }
}

const parseMessage = (data: unknown): HostMessage | null => {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    return null
  }
  const message = data as Record<string, unknown>
  if (message.source !== SOURCE || message.v !== 1) return null
  if (message.type === 'leave') return { type: 'leave' }
  return message.type === 'join' ? parseJoin(message) : null
}

type Handlers = {
  onJoin: (join: HostJoin) => void
  onLeave: () => void
}

// The origin of the parent when it is an allowed one, else null
const allowedParentOrigin = () => {
  const origin = parentOrigin()
  const isAllowed = !!origin && allowedOrigins().includes(origin)
  return window.parent !== window && isAllowed ? origin : null
}

// Only the first valid `join` is handed over, and none after a `leave`
// (the call is over: `hasJoined` then means "no more join")
const dispatch = (
  message: HostMessage | null,
  { onJoin, onLeave }: Handlers,
  state: { hasJoined: boolean }
) => {
  if (message?.type === 'leave') {
    state.hasJoined = true
    onLeave()
  }
  if (message?.type !== 'join' || state.hasJoined) return
  state.hasJoined = true
  onJoin({ token: message.token, audio: message.audio, video: message.video })
}

const makeListener = (
  parent: Window,
  origin: string,
  handlers: Handlers
): ((event: MessageEvent) => void) => {
  const state = { hasJoined: false }
  return (event) => {
    if (event.origin !== origin || event.source !== parent) return
    try {
      dispatch(parseMessage(event.data), handlers, state)
    } catch {
      // a hostile message must not reach the page
    }
  }
}

/**
 * Starts listening to the Chat page that frames this page, and tells it the
 * page is ready. Returns null (and says nothing) when the page is not framed
 * by an origin of VITE_EMBED_ALLOWED_ORIGINS.
 *
 * Only the first valid `join` is handed over.
 */
export const startHostBridge = (handlers: Handlers): HostBridge | null => {
  const origin = allowedParentOrigin()
  if (!origin) return null
  const parent = window.parent

  const post = (message: object) =>
    parent.postMessage({ source: SOURCE, v: 1, ...message }, origin)

  const listener = makeListener(parent, origin, handlers)
  window.addEventListener('message', listener)
  post({ type: 'ready' })

  return {
    joined: () => post({ type: 'joined' }),
    left: (reason) => post({ type: 'left', reason }),
    error: (code) => post({ type: 'error', code }),
    close: () => window.removeEventListener('message', listener),
  }
}
