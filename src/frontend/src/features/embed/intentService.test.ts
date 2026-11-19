// @vitest-environment jsdom
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from 'vitest'
import { ApiError } from '@/api/ApiError'
import { fetchApi } from '@/api/fetchApi'
import { authUrl } from '@/features/auth/utils/authUrl'
import { startIntentService } from './intentService'

/*
 * Page /intents?intent=<id>: Meet is the *service* of a cozy intent, served by
 * cozy-stack in an iframe. cozy-interapp replaces the postMessage protocol of
 * /embed. The stack token and the LiveKit token are never logged, stored or
 * put in a URL.
 */

const interapp = vi.hoisted(() => ({
  options: null as null | {
    fetch: (method: string, path: string, body?: unknown) => Promise<unknown>
  },
  constructed: 0,
  createService: vi.fn(),
  service: {
    getData: vi.fn(),
    notifyReadyToUse: vi.fn(),
    sendResult: vi.fn(),
    terminate: vi.fn(),
    throw: vi.fn(),
    cancel: vi.fn(),
    onData: vi.fn(),
  },
}))

vi.mock('cozy-interapp', () => ({
  default: class Intents {
    constructor(options: never) {
      interapp.options = options
      interapp.constructed += 1
    }
    createService = interapp.createService
  },
}))
vi.mock('@/api/fetchApi', () => ({ fetchApi: vi.fn() }))

const INTENT = 'intent-42'
const ROOM = '3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11'
const LIVEKIT = 'eyJ.secret-livekit-token.sig'
const STACK = 'stack-access-token-secret'
const PLATFORM = 'https://alice.twake.example.org'
const JOIN_TIMEOUT_MS = 10_000
const SIGNED_IN = { source: 'twake-meet-intent', type: 'signed-in' }

const data = (fields: object = {}) => ({
  room: ROOM,
  token: LIVEKIT,
  audio: true,
  video: false,
  ...fields,
})

// onNeedsSignIn(signIn): a 401, the page shows a button that calls signIn on
// click (a popup needs a user gesture)
// onError: failures before a service exists (no stack token yet, so no
// cozy-interapp to answer through) can only be told to the page itself
const handlers = () => ({
  onJoin: vi.fn(),
  onLeave: vi.fn(),
  onError: vi.fn(),
  onNeedsSignIn: vi.fn(),
})
const flush = () => vi.advanceTimersByTimeAsync(0)
const stackToken = fetchApi as unknown as Mock
const sent = () => interapp.service.sendResult.mock.calls.map(([r]) => r)

const popup = { closed: false, close: vi.fn() }

const start = async (h = handlers()) => {
  const bridge = startIntentService(h)
  await flush()
  return { bridge, h }
}

const answerStackToken = () =>
  stackToken.mockResolvedValue({
    access_token: STACK,
    platform_url: PLATFORM,
  })

const hostSends = (message: unknown) => {
  const cb = interapp.service.onData.mock.calls[0][0]
  cb(message)
}

describe('startIntentService', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    interapp.options = null
    interapp.constructed = 0
    Object.values(interapp.service).forEach((fn) => fn.mockReset())
    interapp.service.getData.mockResolvedValue(data())
    interapp.createService.mockReset()
    interapp.createService.mockResolvedValue(interapp.service)
    stackToken.mockReset()
    answerStackToken()
    popup.closed = false
    popup.close = vi.fn()
    vi.spyOn(window, 'open').mockReturnValue(popup as never)
    window.history.pushState({}, '', `/intents?intent=${INTENT}`)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    window.history.pushState({}, '', '/')
  })

  describe('without an intent', () => {
    it.each(['/intents', '/intents?intent=', '/intents?other=1'])(
      'returns null and asks nothing on %s',
      async (url) => {
        window.history.pushState({}, '', url)
        const h = handlers()
        expect(startIntentService(h)).toBeNull()
        await flush()
        expect(stackToken).not.toHaveBeenCalled()
        expect(interapp.createService).not.toHaveBeenCalled()
      }
    )
  })

  describe('the intent of the URL', () => {
    it.each([
      ['a path', '../files/x'],
      ['a slash', 'a/b'],
      ['a space', 'a b'],
      ['a query', 'a?b'],
      ['a host', '//evil.com'],
      ['a dotted host', '.evil.com'],
      ['nothing', ''],
      ['more than 128 characters', 'a'.repeat(129)],
    ])('is ignored when it holds %s: no call, no service', async (_n, bad) => {
      window.history.pushState(
        {},
        '',
        `/intents?intent=${encodeURIComponent(bad)}`
      )
      const h = handlers()
      expect(startIntentService(h)).toBeNull()
      await flush()
      expect(stackToken).not.toHaveBeenCalled()
      expect(interapp.constructed).toBe(0)
      expect(interapp.createService).not.toHaveBeenCalled()
    })

    it.each(['9761c01d09e19b7797a8ce7dd501c9d7', 'intent-42'])(
      'is served when it is a plain identifier (%s)',
      async (id) => {
        window.history.pushState({}, '', `/intents?intent=${id}`)
        const { bridge } = await start()
        expect(bridge).not.toBeNull()
        expect(interapp.createService).toHaveBeenCalledWith(id, window)
      }
    )
  })

  describe('the stack token', () => {
    it('is asked with a POST on stack-token, through the api helper', async () => {
      await start()
      expect(stackToken).toHaveBeenCalledTimes(1)
      const [path, options] = stackToken.mock.calls[0]
      expect(String(path)).toMatch(/stack-token\/$/)
      expect(options).toMatchObject({ method: 'POST' })
    })

    it('opens the service of the intent of the URL, with the window', async () => {
      await start()
      expect(interapp.constructed).toBe(1)
      expect(interapp.createService).toHaveBeenCalledWith(INTENT, window)
    })

    it('is not asked again once obtained', async () => {
      await start()
      await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS * 2)
      expect(stackToken).toHaveBeenCalledTimes(1)
    })

    it('is sent to the platform, and only there, as a Bearer token', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ data: { id: '1' } }),
      })
      vi.stubGlobal('fetch', fetchMock)
      await start()
      const result = await interapp.options!.fetch('POST', '/intents/x', {
        a: 1,
      })
      expect(result).toEqual({ data: { id: '1' } })
      const [url, init] = fetchMock.mock.calls[0]
      expect(String(url)).toBe(`${PLATFORM}/intents/x`)
      expect(init.method).toBe('POST')
      expect(init.headers.Authorization ?? init.headers.authorization).toBe(
        `Bearer ${STACK}`
      )
      expect(JSON.stringify(init.headers).toLowerCase()).toContain('json')
      expect(JSON.parse(init.body)).toEqual({ a: 1 })
    })

    it.each([
      '.evil.com/x',
      '//evil.com/x',
      '@evil.com',
      'http://evil.com',
      '',
    ])('never leaves the platform: the path %j is refused', async (path) => {
      const fetchMock = vi.fn()
      vi.stubGlobal('fetch', fetchMock)
      await start()
      await expect(interapp.options!.fetch('GET', path)).rejects.toBeDefined()
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('asks exactly platform_url + path for an absolute path of the platform', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      })
      vi.stubGlobal('fetch', fetchMock)
      await start()
      await interapp.options!.fetch('GET', '/intents/abc')
      expect(fetchMock).toHaveBeenCalledTimes(1)
      const [url, init] = fetchMock.mock.calls[0]
      expect(url).toBe(`${PLATFORM}/intents/abc`)
      expect(init.headers.Authorization).toBe(`Bearer ${STACK}`)
    })

    it('is kept out of the URL, of every storage and of the console', async () => {
      const log = vi.spyOn(console, 'log').mockImplementation(() => {})
      const error = vi.spyOn(console, 'error').mockImplementation(() => {})
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      await start()
      const everything = JSON.stringify([
        window.location.href,
        window.name,
        { ...window.localStorage },
        { ...window.sessionStorage },
        log.mock.calls,
        error.mock.calls,
        warn.mock.calls,
      ])
      expect(everything).not.toContain(STACK)
      expect(everything).not.toContain(LIVEKIT)
    })
  })

  describe('getting ready to join', () => {
    it('hands the call the data of the host, then says it is ready, once', async () => {
      const { h } = await start()
      expect(h.onJoin).toHaveBeenCalledTimes(1)
      expect(h.onJoin).toHaveBeenCalledWith(
        expect.objectContaining({
          room: ROOM,
          token: LIVEKIT,
          audio: true,
          video: false,
        })
      )
      expect(interapp.service.notifyReadyToUse).toHaveBeenCalledTimes(1)
    })

    it('keeps displayName out of the way: it is optional', async () => {
      interapp.service.getData.mockResolvedValue(data({ displayName: 'Alice' }))
      const { h } = await start()
      expect(h.onJoin).toHaveBeenCalledTimes(1)
    })

    it('never calls sendResult before getData has answered', async () => {
      let resolve: (value: unknown) => void = () => {}
      interapp.service.getData.mockReturnValue(
        new Promise((r) => {
          resolve = r
        })
      )
      const { h } = await start()
      expect(interapp.service.sendResult).not.toHaveBeenCalled()
      expect(h.onJoin).not.toHaveBeenCalled()
      resolve(data())
      await flush()
      expect(h.onJoin).toHaveBeenCalledTimes(1)
    })

    it.each([
      ['no room', { room: undefined }],
      ['a room that is not a uuid', { room: 'abc-defg-hij' }],
      ['a room that is a number', { room: 42 }],
      ['no token', { token: undefined }],
      ['an empty token', { token: '' }],
      ['a blank token', { token: '   ' }],
      ['a token that is a number', { token: 12 }],
      ['audio as a string', { audio: 'true' }],
      ['no audio', { audio: undefined }],
      ['video as a number', { video: 1 }],
      ['no video', { video: undefined }],
    ])('says unsupported and never joins with %s', async (_name, bad) => {
      interapp.service.getData.mockResolvedValue(data(bad))
      const { h } = await start()
      expect(h.onJoin).not.toHaveBeenCalled()
      expect(interapp.service.notifyReadyToUse).not.toHaveBeenCalled()
      expect(sent()).toEqual([{ event: 'error', code: 'unsupported' }])
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })

    it('logs the error and says unsupported when getData rejects', async () => {
      const error = vi.spyOn(console, 'error').mockImplementation(() => {})
      interapp.service.getData.mockRejectedValue(new Error('boom'))
      const { h } = await start()
      expect(error).toHaveBeenCalled()
      expect(h.onJoin).not.toHaveBeenCalled()
      expect(sent()).toEqual([{ event: 'error', code: 'unsupported' }])
    })

    it.each([null, undefined, 'room', [data()]])(
      'says unsupported when getData answers %j',
      async (answer) => {
        interapp.service.getData.mockResolvedValue(answer)
        const { h } = await start()
        expect(h.onJoin).not.toHaveBeenCalled()
        expect(sent()).toEqual([{ event: 'error', code: 'unsupported' }])
      }
    )
  })

  describe('the service that does not come in time', () => {
    it('never joins when the service is obtained after 10 s, and does not read its data', async () => {
      let resolve: (value: unknown) => void = () => {}
      interapp.createService.mockReturnValue(
        new Promise((r) => {
          resolve = r
        })
      )
      const { h } = await start()
      await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS)
      resolve(interapp.service)
      await flush()
      expect(interapp.service.getData).not.toHaveBeenCalled()
      expect(interapp.service.notifyReadyToUse).not.toHaveBeenCalled()
      expect(h.onJoin).not.toHaveBeenCalled()
      // no service at the deadline: the page is told, the host cannot be
      expect(h.onError).toHaveBeenCalledWith('unsupported')
      // nothing was said before getData
      expect(interapp.service.sendResult).not.toHaveBeenCalled()
    })

    it('still joins with a service obtained just before the timeout', async () => {
      let resolve: (value: unknown) => void = () => {}
      interapp.createService.mockReturnValue(
        new Promise((r) => {
          resolve = r
        })
      )
      const { h } = await start()
      await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS - 1)
      resolve(interapp.service)
      await flush()
      expect(h.onJoin).toHaveBeenCalledTimes(1)
    })

    it('does not time out once the data came', async () => {
      await start()
      await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS * 3)
      expect(sent()).toEqual([])
    })
  })

  describe('telling the host', () => {
    it('sends joined, left (with its reason) and error as results', async () => {
      const { bridge } = await start()
      bridge!.joined()
      expect(sent()).toEqual([{ event: 'joined' }])
      bridge!.left('removed')
      expect(sent()).toEqual([
        { event: 'joined' },
        { event: 'left', reason: 'removed' },
      ])
    })

    it('terminates the service once after left, and not before', async () => {
      const { bridge } = await start()
      bridge!.joined()
      expect(interapp.service.terminate).not.toHaveBeenCalled()
      bridge!.left('user')
      bridge!.left('user')
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })

    it('terminates the service once after an error', async () => {
      const { bridge } = await start()
      bridge!.error('token_rejected')
      expect(sent()).toEqual([{ event: 'error', code: 'token_rejected' }])
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })

    it('says nothing more once terminated', async () => {
      const { bridge } = await start()
      bridge!.left('idle')
      bridge!.joined()
      bridge!.error('unknown')
      expect(sent()).toEqual([{ event: 'left', reason: 'idle' }])
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })

    it('after close, says nothing', async () => {
      const { bridge } = await start()
      bridge!.close()
      bridge!.joined()
      expect(sent()).toEqual([])
    })
  })

  describe('leave from the host', () => {
    it('asks the page to leave when the host sends { leave: true }', async () => {
      const { h } = await start()
      hostSends({ leave: true })
      expect(h.onLeave).toHaveBeenCalledTimes(1)
    })

    it.each([
      null,
      undefined,
      'leave',
      { leave: 'true' },
      { leave: false },
      {},
    ])('ignores %j from the host', async (message) => {
      const { h } = await start()
      hostSends(message)
      expect(h.onLeave).not.toHaveBeenCalled()
    })

    it('never joins when the host leaves before the data came', async () => {
      let resolve: (value: unknown) => void = () => {}
      interapp.service.getData.mockReturnValue(
        new Promise((r) => {
          resolve = r
        })
      )
      const { h } = await start()
      hostSends({ leave: true })
      resolve(data())
      await flush()
      expect(h.onJoin).not.toHaveBeenCalled()
    })
  })

  // A popup opened by async code after a 401 is blocked (no user gesture):
  // the bridge never calls window.open by itself. It hands the page a
  // signIn() through onNeedsSignIn(signIn); the page calls it from a click.
  describe('a session that is missing', () => {
    // Sent by the popup the page opened, unless said otherwise (jsdom takes
    // only a real Window as `source` in the init: set it afterwards)
    const signedInMessage = (
      origin = window.location.origin,
      source: unknown = popup
    ) => {
      const event = new MessageEvent('message', { data: SIGNED_IN, origin })
      Object.defineProperty(event, 'source', { value: source })
      window.dispatchEvent(event)
    }

    beforeEach(() => {
      stackToken.mockRejectedValueOnce(new ApiError(401, {}))
    })

    // starts, then the user clicks (calls the signIn the page was handed)
    const startAndClick = async () => {
      const h = handlers()
      const out = await start(h)
      expect(h.onNeedsSignIn).toHaveBeenCalledTimes(1)
      const signIn = h.onNeedsSignIn.mock.calls[0][0] as () => void
      return { ...out, signIn }
    }

    it('asks the page for a sign-in, and opens no popup by itself', async () => {
      const { h } = await start()
      expect(h.onNeedsSignIn).toHaveBeenCalledTimes(1)
      expect(typeof h.onNeedsSignIn.mock.calls[0][0]).toBe('function')
      await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS * 2)
      expect(window.open).not.toHaveBeenCalled()
      expect(h.onError).not.toHaveBeenCalled()
      expect(h.onJoin).not.toHaveBeenCalled()
      expect(interapp.createService).not.toHaveBeenCalled()
    })

    it('opens the sign-in page of Meet in a popup, coming back to /intents/signed-in, when signIn is called', async () => {
      const { signIn } = await startAndClick()
      signIn()
      expect(window.open).toHaveBeenCalledTimes(1)
      const url = vi.mocked(window.open).mock.calls[0][0]
      expect(String(url)).toBe(
        authUrl({ returnTo: `${window.location.origin}/intents/signed-in` })
      )
      expect(interapp.createService).not.toHaveBeenCalled()
    })

    it('does not ask for the stack token again before signIn is called', async () => {
      await startAndClick()
      await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS)
      expect(stackToken).toHaveBeenCalledTimes(1)
    })

    it('asks again, once, when the popup closes', async () => {
      const { h, signIn } = await startAndClick()
      signIn()
      popup.closed = true
      await vi.advanceTimersByTimeAsync(2_000)
      expect(stackToken).toHaveBeenCalledTimes(2)
      expect(h.onJoin).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS)
      expect(stackToken).toHaveBeenCalledTimes(2)
      expect(window.open).toHaveBeenCalledTimes(1)
    })

    it('asks again, once, on the signed-in message of its own origin', async () => {
      const { h, signIn } = await startAndClick()
      signIn()
      signedInMessage()
      signedInMessage()
      await flush()
      expect(stackToken).toHaveBeenCalledTimes(2)
      expect(h.onJoin).toHaveBeenCalledTimes(1)
    })

    it('ignores a signed-in message of another origin', async () => {
      const { signIn } = await startAndClick()
      signIn()
      signedInMessage('https://evil.example.net')
      await flush()
      expect(stackToken).toHaveBeenCalledTimes(1)
    })

    it('ignores a signed-in message of its own origin from another window than the popup', async () => {
      const { signIn } = await startAndClick()
      signIn()
      signedInMessage(window.location.origin, window)
      signedInMessage(window.location.origin, null)
      await flush()
      expect(stackToken).toHaveBeenCalledTimes(1)
    })

    it('ignores any other message of its own origin', async () => {
      const { signIn } = await startAndClick()
      signIn()
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { ...SIGNED_IN, type: 'other' },
          origin: window.location.origin,
        })
      )
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { source: 'someone', type: 'signed-in' },
          origin: window.location.origin,
        })
      )
      await flush()
      expect(stackToken).toHaveBeenCalledTimes(1)
    })

    it('says unauthenticated, after one retry only, when it is still 401', async () => {
      stackToken.mockReset()
      stackToken.mockRejectedValue(new ApiError(401, {}))
      const { h, signIn } = await startAndClick()
      signIn()
      signedInMessage()
      await flush()
      expect(stackToken).toHaveBeenCalledTimes(2)
      expect(window.open).toHaveBeenCalledTimes(1)
      // the second 401 does not ask for a second sign-in
      expect(h.onNeedsSignIn).toHaveBeenCalledTimes(1)
      expect(h.onError).toHaveBeenCalledTimes(1)
      expect(h.onError).toHaveBeenCalledWith('unauthenticated')
      expect(h.onJoin).not.toHaveBeenCalled()
    })

    it('says popup_blocked when the click opens no popup', async () => {
      vi.mocked(window.open).mockReturnValue(null)
      const { h, signIn } = await startAndClick()
      signIn()
      await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS)
      expect(h.onError).toHaveBeenCalledTimes(1)
      expect(h.onError).toHaveBeenCalledWith('popup_blocked')
      expect(stackToken).toHaveBeenCalledTimes(1)
      expect(h.onJoin).not.toHaveBeenCalled()
    })
  })

  describe('a stack that rejected the id token', () => {
    it('asks the page for a sign-in on a 401 id_token_rejected, as on a bare 401', async () => {
      stackToken.mockReset()
      stackToken.mockRejectedValue(
        new ApiError(401, { error: 'id_token_rejected' })
      )
      const { h } = await start()
      expect(h.onNeedsSignIn).toHaveBeenCalledTimes(1)
      expect(typeof h.onNeedsSignIn.mock.calls[0][0]).toBe('function')
      expect(h.onError).not.toHaveBeenCalled()
      expect(window.open).not.toHaveBeenCalled()
      expect(interapp.createService).not.toHaveBeenCalled()
    })

    it('says unreachable on a 502 stack_exchange_failed, with no sign-in', async () => {
      stackToken.mockReset()
      stackToken.mockRejectedValue(
        new ApiError(502, { error: 'stack_exchange_failed' })
      )
      const { h } = await start()
      expect(h.onError).toHaveBeenCalledWith('unreachable')
      expect(h.onNeedsSignIn).not.toHaveBeenCalled()
      expect(window.open).not.toHaveBeenCalled()
    })
  })

  // A session older than the workplaceFqdn scope has no twake_instance: the
  // server answers 409 no_twake_instance, and signing in again fills it
  describe('a session without a twake instance', () => {
    const noInstance = () => new ApiError(409, { error: 'no_twake_instance' })

    it('asks the page for a sign-in on a 409 no_twake_instance, as on a 401', async () => {
      stackToken.mockReset()
      stackToken.mockRejectedValue(noInstance())
      const { h } = await start()
      expect(h.onNeedsSignIn).toHaveBeenCalledTimes(1)
      expect(typeof h.onNeedsSignIn.mock.calls[0][0]).toBe('function')
      expect(h.onError).not.toHaveBeenCalled()
      expect(window.open).not.toHaveBeenCalled()
      expect(interapp.createService).not.toHaveBeenCalled()
    })

    it('asks again, once, after the sign-in, and joins when the instance is known', async () => {
      stackToken.mockReset()
      stackToken.mockRejectedValueOnce(noInstance())
      answerStackToken()
      const { h } = await start()
      h.onNeedsSignIn.mock.calls[0][0]()
      expect(window.open).toHaveBeenCalledTimes(1)
      popup.closed = true
      await vi.advanceTimersByTimeAsync(2_000)
      expect(stackToken).toHaveBeenCalledTimes(2)
      expect(h.onJoin).toHaveBeenCalledTimes(1)
    })

    it('says unreachable, not unauthenticated, and does not loop, when it is still 409', async () => {
      stackToken.mockReset()
      stackToken.mockRejectedValue(noInstance())
      const { h } = await start()
      h.onNeedsSignIn.mock.calls[0][0]()
      popup.closed = true
      await vi.advanceTimersByTimeAsync(2_000)
      expect(stackToken).toHaveBeenCalledTimes(2)
      expect(h.onNeedsSignIn).toHaveBeenCalledTimes(1)
      expect(window.open).toHaveBeenCalledTimes(1)
      expect(h.onError).toHaveBeenCalledTimes(1)
      expect(h.onError).toHaveBeenCalledWith('unreachable')
    })

    it.each([
      ['another error code', { error: 'other' }],
      ['no error code', {}],
      ['no body', undefined],
    ])('stays unreachable on a 409 with %s', async (_n, body) => {
      stackToken.mockReset()
      stackToken.mockRejectedValue(new ApiError(409, body))
      const { h } = await start()
      expect(h.onError).toHaveBeenCalledWith('unreachable')
      expect(h.onNeedsSignIn).not.toHaveBeenCalled()
      expect(window.open).not.toHaveBeenCalled()
    })
  })

  describe('a stack token that cannot be had', () => {
    it.each([
      [409, 'no twake instance'],
      [400, 'bad request'],
      [502, 'bad gateway'],
    ])('says unreachable on a %s (%s), without a popup', async (status) => {
      stackToken.mockReset()
      stackToken.mockRejectedValue(new ApiError(status, {}))
      const { h } = await start()
      expect(h.onError).toHaveBeenCalledWith('unreachable')
      expect(window.open).not.toHaveBeenCalled()
      expect(h.onJoin).not.toHaveBeenCalled()
      expect(interapp.createService).not.toHaveBeenCalled()
    })

    it('says unreachable on a network failure too', async () => {
      stackToken.mockReset()
      stackToken.mockRejectedValue(new TypeError('Failed to fetch'))
      const { h } = await start()
      expect(h.onError).toHaveBeenCalledWith('unreachable')
      expect(h.onJoin).not.toHaveBeenCalled()
    })
  })
})
