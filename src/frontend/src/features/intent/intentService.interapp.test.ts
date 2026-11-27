// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchApi } from '@/api/fetchApi'
import { startIntentService } from './intentService'

/*
 * The handshake with cozy-stack, through the REAL cozy-interapp (the other
 * specs mock it, so they cannot prove the types and origins that really go
 * to the host, nor the signature of the `fetch` the library calls).
 * Only the network is faked: the stack-token API and the platform
 * (`GET <platform>/intents/<id>`). jsdom has no parent frame (parent ===
 * window), so what the page posts to its parent is recorded on
 * `window.postMessage`, and the host answers with MessageEvents of its origin.
 */

vi.mock('@/api/fetchApi', () => ({ fetchApi: vi.fn() }))

const INTENT = 'intent-42'
const ROOM = '3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11'
const LIVEKIT = 'eyJ.secret-livekit-token.sig'
const STACK = 'stack-access-token-secret'
const PLATFORM = 'https://alice.twake.example.org'
const CLIENT = 'https://alice-chat.twake.example.org'
const EVIL = 'https://evil.example.net'

// What the page posts to its parent: [message, targetOrigin]
const posted = () =>
  vi.mocked(window.postMessage).mock.calls as unknown as [
    { type: string; [key: string]: unknown },
    string,
  ][]
const typesPosted = () => posted().map(([message]) => message.type)

const hostSends = (data: unknown, origin = CLIENT) =>
  window.dispatchEvent(new MessageEvent('message', { data, origin }))

describe('startIntentService through the real cozy-interapp', () => {
  beforeEach(() => {
    window.history.pushState({}, '', `/intents?intent=${INTENT}`)
    vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    vi.mocked(fetchApi).mockResolvedValue({
      access_token: STACK,
      platform_url: PLATFORM,
    })
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          data: { id: INTENT, attributes: { client: CLIENT } },
        }),
      })
    )
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    window.history.pushState({}, '', '/')
  })

  it('reads the intent on the platform, announces ready to the client origin, takes its data, then reports joined and left', async () => {
    const onJoin = vi.fn()
    const handlers = {
      onJoin,
      onLeave: vi.fn(),
      onError: vi.fn(),
    }
    const bridge = startIntentService(handlers)!

    // 1. the intent is read on the platform with the stack token, through
    // the (method, path, body) signature the library calls
    await vi.waitFor(() =>
      expect(typesPosted()).toEqual([`intent-${INTENT}:ready`])
    )
    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${PLATFORM}/intents/${INTENT}`)
    expect(init.method).toBe('GET')
    expect((init.headers as Record<string, string>).Authorization).toBe(
      `Bearer ${STACK}`
    )

    // 2. ready goes to the origin of the client, and to it alone
    expect(posted()[0][1]).toBe(CLIENT)
    expect(onJoin).not.toHaveBeenCalled()

    // data from another origin is not the host's
    hostSends({ room: ROOM, token: 'forged', audio: true, video: true }, EVIL)
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(onJoin).not.toHaveBeenCalled()

    // 3. the host answers with its data: the page joins, then says readyToUse
    hostSends({ room: ROOM, token: LIVEKIT, audio: true, video: false })
    await vi.waitFor(() =>
      expect(onJoin).toHaveBeenCalledWith({
        room: ROOM,
        token: LIVEKIT,
        audio: true,
        video: false,
      })
    )
    await vi.waitFor(() =>
      expect(typesPosted()).toEqual([
        `intent-${INTENT}:ready`,
        `intent-${INTENT}:readyToUse`,
      ])
    )

    // 4. joined, then left: results first, then the intent is terminated
    bridge.joined()
    bridge.left('user')
    expect(posted().slice(2)).toEqual([
      [
        { type: `intent-${INTENT}:result`, result: { event: 'joined' } },
        CLIENT,
      ],
      [
        {
          type: `intent-${INTENT}:result`,
          result: { event: 'left', reason: 'user' },
        },
        CLIENT,
      ],
      [{ type: `intent-${INTENT}:done`, document: undefined }, CLIENT],
    ])
    expect(handlers.onError).not.toHaveBeenCalled()

    // nothing once terminated
    bridge.joined()
    expect(posted()).toHaveLength(5)

    // the stack token and the LiveKit token never travel back to the host
    const everything = JSON.stringify(posted())
    expect(everything).not.toContain(STACK)
    expect(everything).not.toContain(LIVEKIT)

    bridge.close()
  })
})
