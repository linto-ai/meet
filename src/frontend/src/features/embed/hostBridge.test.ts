// @vitest-environment jsdom
import {
  type Mock,
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'
import { type HostJoin, startHostBridge } from './hostBridge'

/*
 * Protocol: twake-meet-embed v1 (Chat page <-> Meet page /embed/<uuid>).
 * The page of Meet runs in an iframe of Chat and only trusts a parent whose
 * origin is in VITE_EMBED_ALLOWED_ORIGINS (read at run time, comma separated,
 * exact origins).
 */

const CHAT = 'https://chat.example.org'
const OTHER = 'https://other.example.org'
const TOKEN = 'eyJ.secret-livekit-token.sig'

const parent = { postMessage: vi.fn() }

const setAllowedOrigins = (value: string | undefined) => {
  Object.assign(window, {
    VITE_CONFIG:
      value === undefined ? {} : { VITE_EMBED_ALLOWED_ORIGINS: value },
  })
}

const setReferrer = (value: string) =>
  vi.spyOn(document, 'referrer', 'get').mockReturnValue(value)

const setAncestorOrigins = (origins: string[]) =>
  Object.defineProperty(window.location, 'ancestorOrigins', {
    configurable: true,
    value: origins,
  })

const send = (
  data: unknown,
  {
    origin = CHAT,
    source = parent as unknown,
  }: { origin?: string; source?: unknown } = {}
) => {
  const event = new MessageEvent('message', { data, origin })
  Object.defineProperty(event, 'source', { value: source })
  window.dispatchEvent(event)
}

const msg = (type: string, fields: object = {}) => ({
  source: 'twake-meet-embed',
  v: 1,
  type,
  ...fields,
})
const join = (fields: object = {}) =>
  msg('join', { token: TOKEN, audio: true, video: false, ...fields })

const posted = () => parent.postMessage.mock.calls

describe('host bridge (twake-meet-embed v1)', () => {
  let onJoin: Mock<(join: HostJoin) => void>
  let onLeave: Mock<() => void>
  const start = () => startHostBridge({ onJoin, onLeave })

  beforeEach(() => {
    parent.postMessage.mockReset()
    onJoin = vi.fn<(join: HostJoin) => void>()
    onLeave = vi.fn<() => void>()
    Object.defineProperty(window, 'parent', {
      configurable: true,
      value: parent,
    })
    setAllowedOrigins(`${CHAT}, ${OTHER}`)
    setReferrer(`${CHAT}/rooms/!abc:example.org`)
    setAncestorOrigins([])
  })

  afterEach(() => {
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'parent')
    Reflect.deleteProperty(window.location, 'ancestorOrigins')
  })

  describe('announcing itself', () => {
    it('tells the parent it is ready, once, at the origin of the parent', () => {
      start()
      expect(posted()).toEqual([[msg('ready'), CHAT]])
    })

    it('prefers the ancestor origin over the referrer', () => {
      setAncestorOrigins([OTHER])
      setReferrer(`${CHAT}/`)
      start()
      expect(posted()).toEqual([[msg('ready'), OTHER]])
    })

    it('says nothing when the parent origin is not in the allow-list', () => {
      setReferrer('https://evil.example.net/page')
      expect(start()).toBeNull()
      expect(posted()).toEqual([])
    })

    it('says nothing when the referrer is not a URL', () => {
      setReferrer('not a url')
      expect(start()).toBeNull()
      setReferrer('')
      expect(start()).toBeNull()
      expect(posted()).toEqual([])
    })

    it('says nothing when the allow-list is not configured', () => {
      setAllowedOrigins(undefined)
      expect(start()).toBeNull()
      expect(posted()).toEqual([])
    })

    it('does not match an origin by suffix or prefix', () => {
      setReferrer('https://evil-chat.example.org/')
      expect(start()).toBeNull()
      setReferrer('https://chat.example.org.evil.net/')
      expect(start()).toBeNull()
      expect(posted()).toEqual([])
    })

    it('says nothing when the page is not in a frame', () => {
      Object.defineProperty(window, 'parent', {
        configurable: true,
        value: window,
      })
      expect(start()).toBeNull()
    })

    it('never uses the wildcard target origin', () => {
      const bridge = start()!
      bridge.joined()
      bridge.left('user')
      bridge.error('unknown')
      expect(posted().length).toBe(4)
      expect(posted().every(([, target]) => target === CHAT)).toBe(true)
    })
  })

  describe('receiving the join of the parent', () => {
    it('hands over the token and the media choices of a valid join', () => {
      start()
      send(join({ audio: false, video: true }))
      expect(onJoin).toHaveBeenCalledTimes(1)
      expect(onJoin).toHaveBeenCalledWith({
        token: TOKEN,
        audio: false,
        video: true,
      })
    })

    it('accepts a join from the parent, spaces around its origin in the allow-list ignored', () => {
      setAllowedOrigins(`  ${CHAT}  ,${OTHER}`)
      start()
      send(join(), { origin: CHAT })
      expect(onJoin).toHaveBeenCalledTimes(1)
    })

    it('ignores a join from an allowed origin that is not the parent', () => {
      start()
      send(join(), { origin: OTHER })
      expect(onJoin).not.toHaveBeenCalled()
    })

    it('hands over the first valid join only', () => {
      start()
      send(join({ audio: true }))
      send(join({ audio: false, token: 'second' }))
      expect(onJoin).toHaveBeenCalledTimes(1)
      expect(onJoin).toHaveBeenCalledWith(
        expect.objectContaining({ token: TOKEN })
      )
    })

    it('still takes a valid join after an invalid one', () => {
      start()
      send(join({ token: '' }))
      send(join())
      expect(onJoin).toHaveBeenCalledTimes(1)
    })

    it.each([
      [
        'an origin out of the allow-list',
        { origin: 'https://evil.example.net' },
      ],
      [
        'a sub-domain of an allowed origin',
        { origin: 'https://a.chat.example.org' },
      ],
      ['an opaque origin', { origin: 'null' }],
      ['a window that is not the parent', { source: { postMessage: vi.fn() } }],
      ['no source', { source: null }],
    ])('ignores a join coming from %s', (_name, where) => {
      start()
      send(join(), where)
      expect(onJoin).not.toHaveBeenCalled()
    })

    it.each([
      ['another source tag', { source: 'something-else' }],
      ['no source tag', { source: undefined }],
      ['version 2', { v: 2 }],
      ['no version', { v: undefined }],
      ['version as a string', { v: '1' }],
      ['an unknown type', { type: 'jion' }],
    ])('ignores a message with %s', (_name, override) => {
      start()
      send({ ...join(), ...override })
      expect(onJoin).not.toHaveBeenCalled()
      expect(onLeave).not.toHaveBeenCalled()
    })

    it.each([
      ['an empty token', { token: '' }],
      ['a blank token', { token: '   ' }],
      ['a token that is not a string', { token: 42 }],
      ['no token', { token: undefined }],
      ['audio as a string', { audio: 'true' }],
      ['audio missing', { audio: undefined }],
      ['video as a number', { video: 1 }],
      ['video missing', { video: undefined }],
    ])('ignores a join with %s', (_name, override) => {
      start()
      send({ ...join(), ...override })
      expect(onJoin).not.toHaveBeenCalled()
    })

    it.each([null, undefined, 'join', 7, [], [join()]])(
      'ignores a payload that is not a message (%j)',
      (payload) => {
        start()
        send(payload)
        expect(onJoin).not.toHaveBeenCalled()
        expect(onLeave).not.toHaveBeenCalled()
      }
    )

    it('ignores bad messages without any exception reaching the page', () => {
      const failures = vi.fn()
      window.addEventListener('error', failures)
      start()
      send(null)
      send(join({ token: 42 }))
      send({
        get type() {
          throw new Error('hostile getter')
        },
      })
      window.removeEventListener('error', failures)
      expect(failures).not.toHaveBeenCalled()
    })

    it('never writes the token in the console, even for a rejected message', () => {
      const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map(
        (level) => vi.spyOn(console, level)
      )
      start()
      send(join({ audio: 'oops' }))
      send(join(), { origin: 'https://evil.example.net' })
      send(join())
      const written = spies.flatMap((spy) => spy.mock.calls).flat()
      expect(JSON.stringify(written)).not.toContain(TOKEN)
    })
  })

  describe('receiving leave', () => {
    it('asks to hang up when the parent sends leave', () => {
      start()
      send(msg('leave'))
      expect(onLeave).toHaveBeenCalledTimes(1)
    })

    it('passes a leave that arrives before the join, without any join', () => {
      start()
      send(msg('leave'))
      expect(onLeave).toHaveBeenCalledTimes(1)
      expect(onJoin).not.toHaveBeenCalled()
      expect(posted()).toEqual([[msg('ready'), CHAT]])
    })

    it('ignores a leave from another origin or another window', () => {
      start()
      send(msg('leave'), { origin: 'https://evil.example.net' })
      send(msg('leave'), { source: { postMessage: vi.fn() } })
      expect(onLeave).not.toHaveBeenCalled()
    })
  })

  describe('reporting to the parent', () => {
    it('posts joined to the parent origin', () => {
      const bridge = start()!
      parent.postMessage.mockClear()
      bridge.joined()
      expect(posted()).toEqual([[msg('joined'), CHAT]])
    })

    it.each(['user', 'idle', 'removed', 'disconnected'] as const)(
      'posts left with the reason %s',
      (reason) => {
        const bridge = start()!
        parent.postMessage.mockClear()
        bridge.left(reason)
        expect(posted()).toEqual([[msg('left', { reason }), CHAT]])
      }
    )

    it.each([
      'unsupported',
      'token_rejected',
      'unreachable',
      'unknown',
    ] as const)('posts error with the code %s', (code) => {
      const bridge = start()!
      parent.postMessage.mockClear()
      bridge.error(code)
      expect(posted()).toEqual([[msg('error', { code }), CHAT]])
    })

    it('never puts the token in a message it sends', () => {
      const bridge = start()!
      send(join())
      bridge.joined()
      bridge.left('user')
      expect(JSON.stringify(posted())).not.toContain(TOKEN)
    })
  })

  describe('closing', () => {
    it('stops listening once closed', () => {
      const bridge = start()!
      bridge.close()
      send(join())
      send(msg('leave'))
      expect(onJoin).not.toHaveBeenCalled()
      expect(onLeave).not.toHaveBeenCalled()
    })
  })
})
