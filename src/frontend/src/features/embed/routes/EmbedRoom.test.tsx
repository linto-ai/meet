// @vitest-environment jsdom
import { type ReactNode } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen } from '@testing-library/react'
import {
  ConnectionError,
  ConnectionErrorReason,
  DisconnectReason,
} from 'livekit-client'
import { Route } from 'wouter'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '@/api/queryClient'
import { fetchApi } from '@/api/fetchApi'
import { navigateTo } from '@/navigation/navigateTo'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { routes } from '@/routes'
import { useEmbedIdleLeave } from '../EmbedIdleContext'
import EmbedRoom from './EmbedRoom'

/*
 * /embed/<uuid>: Meet in an iframe of Chat, joined with a LiveKit token that
 * Chat already holds (protocol twake-meet-embed v1). No Meet session, no
 * pre-join screen, no redirect. LiveKit itself, the in-call UI and the other
 * heavy neighbours are replaced by stubs; what is checked is what the page
 * does with the messages of the parent.
 */

const idle = vi.hoisted(() => ({ leave: null as (() => void) | null }))

const livekit = vi.hoisted(() => ({
  props: {} as Record<string, never>,
  room: {
    prepareConnection: () => Promise.resolve(),
    disconnect: () => Promise.resolve(),
    numParticipants: 1,
    localParticipant: { setMicrophoneEnabled: () => Promise.resolve() },
  },
  // one room by default; a test replaces it to see a room recreated
  create: null as null | (() => unknown),
}))

// What /config/ answers; undefined while it loads
const apiConfig = vi.hoisted(() => ({
  data: undefined as unknown,
}))

vi.mock('@livekit/components-react', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  LiveKitRoom: (props: { children?: ReactNode }) => {
    livekit.props = props as never
    return <div data-testid="livekit-room">{props.children}</div>
  },
  usePersistentUserChoices: () => ({
    userChoices: {
      audioEnabled: true,
      videoEnabled: false,
      audioDeviceId: '',
      videoDeviceId: '',
      audioOutputDeviceId: '',
    },
  }),
}))
vi.mock('@/features/rooms/livekit/utils/reconnectPolicy', () => ({
  createLiveKitRoom: () => livekit.create?.() ?? livekit.room,
}))
vi.mock('@/features/rooms/livekit/prefabs/VideoConference', () => ({
  VideoConference: () => {
    const room = useRoomData()
    idle.leave = useEmbedIdleLeave()
    return (
      <div data-testid="video-conference">
        {room ? `${room.id}|${room.livekit?.token}` : 'no room data'}
      </div>
    )
  },
}))
vi.mock('@/features/rooms/components/InviteDialog', () => ({
  InviteDialog: () => <div data-testid="invite-dialog" />,
}))
vi.mock('@/features/pip/components/PictureInPictureConference', () => ({
  PictureInPictureConference: () => <div data-testid="picture-in-picture" />,
}))
vi.mock('@/features/rooms/components/WatchMediaDeviceErrors', () => ({
  WatchMediaDeviceErrors: () => null,
}))
vi.mock('@/features/devtools', () => ({ MeetDevtools: () => null }))
vi.mock('@/features/rooms/livekit/components/blur', () => ({
  BackgroundProcessorFactory: { fromProcessorConfig: () => undefined },
}))
vi.mock('@/features/analytics/telemetry', () => ({
  captureEvent: vi.fn(),
  captureMediaEvent: vi.fn(),
  reportError: vi.fn(),
}))
vi.mock('@/api/useConfig', () => ({
  useConfig: () => ({ data: apiConfig.data }),
}))
vi.mock('@/api/fetchApi', () => ({
  fetchApi: vi.fn(() => Promise.reject(new Error('no network in this test'))),
}))
vi.mock('@/navigation/navigateTo', () => ({ navigateTo: vi.fn() }))

const loadedConfig = (codec = 'vp9') => ({
  auto_mute_on_join_threshold: 50,
  livekit: {
    url: 'wss://livekit.example.org',
    force_wss_protocol: false,
    enable_firefox_proxy_workaround: false,
    default_sources: [],
    default_video_codec: codec,
  },
})

const UUID = '3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11'
const CHAT = 'https://chat.example.org'
const TOKEN = 'eyJ.secret-livekit-token.sig'
const JOIN_TIMEOUT_MS = 10_000
// DISCONNECT_GRACE_MS of EmbedRoom: a bare disconnection before joined waits
// this long for the precise error that LiveKit may still send
const DISCONNECT_GRACE_MS = 1_000

const parent = { postMessage: vi.fn() }

const send = (
  data: unknown,
  {
    origin = CHAT,
    source = parent as unknown,
  }: { origin?: string; source?: unknown } = {}
) => {
  const event = new MessageEvent('message', { data, origin })
  Object.defineProperty(event, 'source', { value: source })
  act(() => {
    window.dispatchEvent(event)
  })
}
const msg = (type: string, fields: object = {}) => ({
  source: 'twake-meet-embed',
  v: 1,
  type,
  ...fields,
})
const sendJoin = (fields: object = {}) =>
  send(msg('join', { token: TOKEN, audio: true, video: false, ...fields }))

const posted = () => parent.postMessage.mock.calls.map(([message]) => message)
const postedTypes = () => posted().map((m) => m.type)

const renderEmbed = () =>
  render(
    <QueryClientProvider client={queryClient}>
      <Route path={routes.embedRoom.path}>
        <EmbedRoom />
      </Route>
    </QueryClientProvider>
  )

// ConnectionError has a protected constructor
class TestConnectionError extends ConnectionError {
  constructor(message: string, reason: ConnectionErrorReason, status?: number) {
    super(message, reason, status)
  }
}

// Hand-made LiveKit events: the stub of <LiveKitRoom> keeps the callbacks.
const lk = () =>
  livekit.props as unknown as {
    token?: string
    serverUrl?: string
    audio?: unknown
    video?: unknown
    connect?: boolean
    onConnected: () => void
    onDisconnected: (reason?: DisconnectReason) => void
    onError: (error: Error) => void
  }

describe('/embed/:uuid', () => {
  beforeEach(() => {
    parent.postMessage.mockReset()
    livekit.props = {}
    livekit.create = null
    apiConfig.data = loadedConfig()
    idle.leave = null
    livekit.room.disconnect = vi.fn(() => Promise.resolve())
    Object.defineProperty(window, 'parent', {
      configurable: true,
      value: parent,
    })
    Object.assign(window, {
      VITE_CONFIG: { VITE_EMBED_ALLOWED_ORIGINS: CHAT },
    })
    vi.spyOn(document, 'referrer', 'get').mockReturnValue(`${CHAT}/rooms/1`)
    window.history.pushState({}, '', `/embed/${UUID}`)
  })

  afterEach(() => {
    cleanup()
    queryClient.clear()
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.mocked(navigateTo).mockClear()
    vi.mocked(fetchApi).mockClear()
    Reflect.deleteProperty(window, 'parent')
    window.history.pushState({}, '', '/')
  })

  describe('theme of the parent (join.theme)', () => {
    const page = () => document.documentElement
    beforeEach(() => page().setAttribute('data-lk-theme', 'visio-light'))
    afterEach(() => page().setAttribute('data-lk-theme', 'visio-light'))

    it('shows the page dark when Chat is dark', () => {
      renderEmbed()
      sendJoin({ theme: 'dark' })
      expect(page().getAttribute('data-lk-theme')).toBe('visio-dark')
      expect(page().style.colorScheme).toBe('dark')
    })

    it('shows the page light when Chat is light', () => {
      renderEmbed()
      sendJoin({ theme: 'light' })
      expect(page().getAttribute('data-lk-theme')).toBe('visio-light')
      expect(page().style.colorScheme).toBe('light')
    })

    it("keeps today's page when the join has no theme", () => {
      renderEmbed()
      sendJoin()
      expect(page().getAttribute('data-lk-theme')).toBe('visio-light')
      expect(screen.getByTestId('livekit-room')).toBeTruthy()
    })

    it("keeps today's page, and joins, when the theme is unknown", () => {
      renderEmbed()
      sendJoin({ theme: 'sepia' })
      expect(page().getAttribute('data-lk-theme')).toBe('visio-light')
      expect(screen.getByTestId('livekit-room')).toBeTruthy()
    })
  })

  describe('before the parent sends join', () => {
    it('announces itself and shows no call, no join screen', () => {
      renderEmbed()
      expect(postedTypes()).toEqual(['ready'])
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(screen.queryByRole('button')).toBeNull()
    })

    it('answers unsupported when no join arrives in time, and nothing else', () => {
      vi.useFakeTimers()
      renderEmbed()
      act(() => {
        vi.advanceTimersByTime(JOIN_TIMEOUT_MS - 1)
      })
      expect(postedTypes()).toEqual(['ready'])
      act(() => {
        vi.advanceTimersByTime(1)
      })
      expect(posted().slice(1)).toEqual([msg('error', { code: 'unsupported' })])
      expect(screen.queryByTestId('livekit-room')).toBeNull()
    })

    it('ignores a join that comes after the unsupported error', () => {
      vi.useFakeTimers()
      renderEmbed()
      act(() => {
        vi.advanceTimersByTime(JOIN_TIMEOUT_MS)
      })
      sendJoin()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(lk().token).toBeUndefined()
      expect(postedTypes()).toEqual(['ready', 'error'])
    })

    it('still joins with a join that comes just before the timeout', () => {
      vi.useFakeTimers()
      renderEmbed()
      act(() => {
        vi.advanceTimersByTime(JOIN_TIMEOUT_MS - 1)
      })
      sendJoin()
      expect(screen.getByTestId('livekit-room')).toBeTruthy()
      expect(lk().token).toBe(TOKEN)
    })

    it('never connects when the parent leaves before it joins', () => {
      renderEmbed()
      send(msg('leave'))
      sendJoin()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(lk().token).toBeUndefined()
      expect(postedTypes()).not.toContain('joined')
    })

    it('does not time out once the join has arrived', () => {
      vi.useFakeTimers()
      renderEmbed()
      sendJoin()
      act(() => {
        vi.advanceTimersByTime(JOIN_TIMEOUT_MS * 3)
      })
      expect(postedTypes()).not.toContain('error')
    })
  })

  describe('the parent is not allowed', () => {
    beforeEach(() => {
      vi.spyOn(document, 'referrer', 'get').mockReturnValue(
        'https://evil.example.net/'
      )
    })

    it('posts nothing, to nobody, and never joins', () => {
      vi.useFakeTimers()
      renderEmbed()
      send(msg('join', { token: TOKEN, audio: true, video: true }), {
        origin: 'https://evil.example.net',
      })
      sendJoin()
      act(() => {
        vi.advanceTimersByTime(JOIN_TIMEOUT_MS * 2)
      })
      expect(parent.postMessage).not.toHaveBeenCalled()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
    })
  })

  describe('joining with the token of the parent', () => {
    it('connects LiveKit with that token on the server of its own /config/', () => {
      renderEmbed()
      sendJoin({ url: 'wss://evil.example.net', serverUrl: 'wss://evil' })
      expect(screen.getByTestId('livekit-room')).toBeTruthy()
      expect(lk().token).toBe(TOKEN)
      expect(lk().serverUrl).toBe('wss://livekit.example.org')
    })

    it('gives the call the room of the uuid and the token, without asking the API', () => {
      renderEmbed()
      sendJoin()
      expect(screen.getByTestId('video-conference').textContent).toBe(
        `${UUID}|${TOKEN}`
      )
      const paths = vi.mocked(fetchApi).mock.calls.map(([path]) => path)
      expect(paths.filter((path) => String(path).includes('/rooms/'))).toEqual(
        []
      )
    })

    it('takes microphone and camera from the join, not from stored choices', () => {
      renderEmbed()
      sendJoin({ audio: false, video: true })
      expect(lk().audio).toBe(false)
      expect(lk().video).toBeTruthy()
    })

    it('can join with the camera off and the microphone on', () => {
      renderEmbed()
      sendJoin({ audio: true, video: false })
      expect(lk().audio).toBe(true)
      expect(lk().video).toBeFalsy()
    })

    it('shows no invitation dialog and no picture-in-picture', () => {
      renderEmbed()
      sendJoin()
      expect(screen.getByTestId('video-conference')).toBeTruthy()
      expect(screen.queryByTestId('invite-dialog')).toBeNull()
      expect(screen.queryByTestId('picture-in-picture')).toBeNull()
    })

    it('keeps the token out of the URL and of every storage', () => {
      renderEmbed()
      sendJoin()
      const everything = JSON.stringify([
        window.location.href,
        window.name,
        { ...window.localStorage },
        { ...window.sessionStorage },
      ])
      expect(everything).not.toContain(TOKEN)
    })

    it('says joined when LiveKit is connected, not before', () => {
      renderEmbed()
      sendJoin()
      expect(postedTypes()).toEqual(['ready'])
      act(() => lk().onConnected())
      expect(posted().slice(1)).toEqual([msg('joined')])
    })
  })

  describe('leaving', () => {
    it('hangs up on leave from the parent and says left (user), once', () => {
      renderEmbed()
      sendJoin()
      act(() => lk().onConnected())
      send(msg('leave'))
      expect(livekit.room.disconnect).toHaveBeenCalled()
      // LiveKit then reports the disconnection it was asked for
      act(() => lk().onDisconnected?.(DisconnectReason.CLIENT_INITIATED))
      expect(posted().filter((m) => m.type === 'left')).toEqual([
        msg('left', { reason: 'user' }),
      ])
    })

    /*
     * leave between join and joined: the connection in progress is dropped.
     * LiveKit may still report onConnected afterwards (the connect was not
     * aborted in time); the page must not enter the room nor say joined.
     */
    describe('a leave that arrives between join and joined', () => {
      it('abandons the connection in progress', async () => {
        renderEmbed()
        sendJoin()
        // the connection is warmed up, then really under way
        await act(async () => {})
        expect(lk().connect).toBe(true)
        send(msg('leave'))
        expect(livekit.room.disconnect).toHaveBeenCalled()
        expect(lk().connect).toBe(false)
      })

      it('never says joined, even if LiveKit connects after the leave', () => {
        renderEmbed()
        sendJoin()
        send(msg('leave'))
        act(() => lk().onConnected?.())
        expect(postedTypes()).toEqual(['ready'])
      })

      it('says neither left nor error when LiveKit connects, then disconnects', async () => {
        vi.useFakeTimers()
        renderEmbed()
        sendJoin()
        send(msg('leave'))
        act(() => lk().onConnected?.())
        act(() => lk().onDisconnected?.(DisconnectReason.CLIENT_INITIATED))
        await act(async () => {
          await vi.advanceTimersByTimeAsync(DISCONNECT_GRACE_MS * 10)
        })
        expect(postedTypes()).toEqual(['ready'])
      })

      it('ignores a join that comes after the leave', () => {
        renderEmbed()
        sendJoin()
        send(msg('leave'))
        sendJoin({ token: 'other.token.value' })
        act(() => lk().onConnected?.())
        expect(lk().token).not.toBe('other.token.value')
        expect(postedTypes()).not.toContain('joined')
      })
    })

    it('does nothing on a leave that arrives before the join', () => {
      renderEmbed()
      send(msg('leave'))
      expect(postedTypes()).toEqual(['ready'])
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(livekit.room.disconnect).not.toHaveBeenCalled()
    })

    it('says left (idle) and shows nothing when the page is idle, without /feedback', () => {
      renderEmbed()
      sendJoin()
      act(() => lk().onConnected())
      act(() => idle.leave?.())
      expect(posted().slice(2)).toEqual([msg('left', { reason: 'idle' })])
      expect(navigateTo).not.toHaveBeenCalled()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
    })

    it('ignores leave from an origin that is not allowed', () => {
      renderEmbed()
      sendJoin()
      send(msg('leave'), { origin: 'https://evil.example.net' })
      expect(livekit.room.disconnect).not.toHaveBeenCalled()
      expect(postedTypes()).not.toContain('left')
    })

    it.each([
      [DisconnectReason.CLIENT_INITIATED, 'user'],
      [DisconnectReason.PARTICIPANT_REMOVED, 'removed'],
      [DisconnectReason.UNKNOWN_REASON, 'disconnected'],
      [DisconnectReason.SERVER_SHUTDOWN, 'disconnected'],
      [undefined, 'disconnected'],
    ])('maps the LiveKit disconnect reason %s to left (%s)', (why, reason) => {
      renderEmbed()
      sendJoin()
      act(() => lk().onConnected())
      act(() => lk().onDisconnected(why))
      expect(posted().slice(2)).toEqual([msg('left', { reason })])
    })

    it('shows nothing after the call and never goes to /feedback', () => {
      renderEmbed()
      sendJoin()
      act(() => lk().onConnected())
      act(() => lk().onDisconnected(DisconnectReason.CLIENT_INITIATED))
      expect(navigateTo).not.toHaveBeenCalled()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(document.body.textContent).toBe('')
    })
  })

  describe('telling the parent only once', () => {
    it('says left a single time when idle is followed by a disconnection', () => {
      renderEmbed()
      sendJoin()
      act(() => lk().onConnected())
      act(() => idle.leave?.())
      act(() => lk().onDisconnected?.(DisconnectReason.CLIENT_INITIATED))
      expect(posted().filter((m) => m.type === 'left')).toEqual([
        msg('left', { reason: 'idle' }),
      ])
    })
  })

  describe('when the join fails', () => {
    it('says unknown for an error that is not a LiveKit connection error', () => {
      renderEmbed()
      sendJoin()
      act(() => lk().onError(new Error('boom')))
      expect(posted().slice(1)).toEqual([msg('error', { code: 'unknown' })])
    })

    it('says unknown for any other connection failure', () => {
      renderEmbed()
      sendJoin()
      act(() =>
        lk().onError(
          new TestConnectionError('other', ConnectionErrorReason.InternalError)
        )
      )
      expect(posted().slice(1)).toEqual([msg('error', { code: 'unknown' })])
    })

    it('says nothing when the camera or microphone is refused', () => {
      renderEmbed()
      sendJoin()
      act(() =>
        lk().onError(Object.assign(new Error('x'), { name: 'NotAllowedError' }))
      )
      expect(postedTypes()).toEqual(['ready'])
    })

    it('says token_rejected when LiveKit refuses the token', () => {
      renderEmbed()
      sendJoin()
      act(() =>
        lk().onError(
          new TestConnectionError(
            'not allowed',
            ConnectionErrorReason.NotAllowed,
            401
          )
        )
      )
      expect(posted().slice(1)).toEqual([
        msg('error', { code: 'token_rejected' }),
      ])
    })

    it('says unreachable when the LiveKit server cannot be reached', () => {
      renderEmbed()
      sendJoin()
      act(() =>
        lk().onError(
          new TestConnectionError(
            'no server',
            ConnectionErrorReason.ServerUnreachable
          )
        )
      )
      expect(posted().slice(1)).toEqual([msg('error', { code: 'unreachable' })])
    })
  })

  /*
   * LiveKit calls onDisconnected after a connection that failed, before or
   * after onError. A failed join says error{code} alone: left only has a
   * meaning once joined was said.
   */
  describe('a join that fails says error, never left', () => {
    const notAllowed = () =>
      new TestConnectionError(
        'not allowed',
        ConnectionErrorReason.NotAllowed,
        401
      )
    const unreachable = () =>
      new TestConnectionError(
        'no server',
        ConnectionErrorReason.ServerUnreachable
      )
    // A bare disconnection starts a grace period (fake clock): nothing is
    // posted before it ends, a single error{unreachable} after
    const bareDisconnect = (why?: DisconnectReason) =>
      act(async () => lk().onDisconnected(why))
    const advance = (ms: number) =>
      act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    const expectOnlyUnreachableAfterGrace = async () => {
      await advance(DISCONNECT_GRACE_MS - 1)
      expect(posted().slice(1)).toEqual([])
      await advance(1)
      expect(posted().slice(1)).toEqual([msg('error', { code: 'unreachable' })])
    }

    it.each([
      ['a rejected token', notAllowed, 'token_rejected'],
      ['an unreachable server', unreachable, 'unreachable'],
    ])(
      '%s: the disconnection that follows the error says nothing',
      (_, make, code) => {
        renderEmbed()
        sendJoin()
        act(() => lk().onError(make()))
        act(() => lk().onDisconnected(undefined))
        expect(posted().slice(1)).toEqual([msg('error', { code })])
      }
    )

    it.each([
      ['a rejected token', notAllowed, 'token_rejected'],
      ['an unreachable server', unreachable, 'unreachable'],
    ])(
      '%s: the disconnection that comes before the error says nothing',
      (_, make, code) => {
        renderEmbed()
        sendJoin()
        act(() => lk().onDisconnected(undefined))
        act(() => lk().onError(make()))
        expect(posted().slice(1)).toEqual([msg('error', { code })])
      }
    )

    it('a rejected token never posts left, whatever the disconnect reason', () => {
      renderEmbed()
      sendJoin()
      act(() => lk().onError(notAllowed()))
      act(() => lk().onDisconnected(DisconnectReason.CLIENT_INITIATED))
      expect(postedTypes()).not.toContain('left')
    })

    /*
     * D5 bis: LiveKit may close the signalling right after the first answer of
     * the server, before joined, with no ConnectionError. The parent must not
     * wait for its own timeout: one error{unreachable} (the closest code of
     * the protocol: the server did not hold the connection), never left.
     */
    it.each([
      ['no reason', undefined],
      ['CLIENT_INITIATED', DisconnectReason.CLIENT_INITIATED],
      ['SERVER_SHUTDOWN', DisconnectReason.SERVER_SHUTDOWN],
      ['UNKNOWN_REASON', DisconnectReason.UNKNOWN_REASON],
      ['JOIN_FAILURE', DisconnectReason.JOIN_FAILURE],
      ['PARTICIPANT_REMOVED', DisconnectReason.PARTICIPANT_REMOVED],
    ])(
      'a disconnection before joined with %s, and no ConnectionError, says error unreachable and nothing else',
      async (_, why) => {
        vi.useFakeTimers()
        renderEmbed()
        sendJoin()
        await bareDisconnect(why)
        await expectOnlyUnreachableAfterGrace()
        await advance(60_000)
        expect(posted().slice(1)).toHaveLength(1)
      }
    )

    it('says the failed join once when LiveKit disconnects twice before joined', async () => {
      vi.useFakeTimers()
      renderEmbed()
      sendJoin()
      await bareDisconnect(undefined)
      await advance(DISCONNECT_GRACE_MS / 2)
      await bareDisconnect(DisconnectReason.SERVER_SHUTDOWN)
      // the grace of the first disconnection decides, the second adds none
      await advance(DISCONNECT_GRACE_MS / 2 - 1)
      expect(posted().slice(1)).toEqual([])
      await advance(1)
      expect(posted().slice(1)).toEqual([msg('error', { code: 'unreachable' })])
      await advance(60_000)
      expect(posted().slice(1)).toHaveLength(1)
    })

    /*
     * The precise ConnectionError comes from the network, after the bare
     * disconnection: the first message wins, so the page waits for it.
     */
    it('a rejected token that arrives during the grace is the only message, token_rejected', async () => {
      vi.useFakeTimers()
      renderEmbed()
      sendJoin()
      await bareDisconnect(undefined)
      await advance(DISCONNECT_GRACE_MS / 2)
      act(() => lk().onError(notAllowed()))
      expect(posted().slice(1)).toEqual([
        msg('error', { code: 'token_rejected' }),
      ])
      await advance(DISCONNECT_GRACE_MS * 10)
      expect(posted().slice(1)).toEqual([
        msg('error', { code: 'token_rejected' }),
      ])
    })

    it('joined during the grace cancels it: no error, now or later', async () => {
      vi.useFakeTimers()
      renderEmbed()
      sendJoin()
      await bareDisconnect(undefined)
      await advance(DISCONNECT_GRACE_MS / 2)
      act(() => lk().onConnected())
      await advance(DISCONNECT_GRACE_MS * 10)
      expect(postedTypes().slice(1)).toEqual(['joined'])
    })

    it('leave from the parent during the grace cancels it: nothing is posted because of it', async () => {
      vi.useFakeTimers()
      renderEmbed()
      sendJoin()
      await bareDisconnect(undefined)
      send(msg('leave'))
      await advance(DISCONNECT_GRACE_MS * 10)
      expect(posted().slice(1)).toEqual([])
    })

    it('unmounting during the grace cancels it: nothing is posted', async () => {
      vi.useFakeTimers()
      const view = renderEmbed()
      sendJoin()
      await bareDisconnect(undefined)
      view.unmount()
      await advance(DISCONNECT_GRACE_MS * 10)
      expect(posted().slice(1)).toEqual([])
    })

    it('does not double an unknown error with the disconnection that follows', () => {
      renderEmbed()
      sendJoin()
      act(() => lk().onError(new Error('boom')))
      act(() => lk().onDisconnected(undefined))
      expect(posted().slice(1)).toEqual([msg('error', { code: 'unknown' })])
    })

    it('a benign device refusal does not count as the error of the disconnection', async () => {
      vi.useFakeTimers()
      renderEmbed()
      sendJoin()
      act(() =>
        lk().onError(Object.assign(new Error('x'), { name: 'NotAllowedError' }))
      )
      await bareDisconnect(undefined)
      await expectOnlyUnreachableAfterGrace()
    })

    it('the disconnection the parent asked for with leave, before joined, says nothing', async () => {
      vi.useFakeTimers()
      renderEmbed()
      sendJoin()
      send(msg('leave'))
      await act(async () =>
        lk().onDisconnected(DisconnectReason.CLIENT_INITIATED)
      )
      expect(livekit.room.disconnect).toHaveBeenCalled()
      await advance(DISCONNECT_GRACE_MS * 10)
      expect(posted().slice(1)).toEqual([])
    })

    /*
     * <Conference> is mounted while /config/ loads and makes a new room when
     * it arrives; the room thrown away may report CLIENT_INITIATED. Only the
     * disconnection of the current room is a failed join.
     */
    describe('when the room is replaced as /config/ arrives', () => {
      const replaceRoom = () => {
        apiConfig.data = undefined
        const create = vi.fn(() => ({ ...livekit.room, disconnect: vi.fn() }))
        livekit.create = create
        const view = renderEmbed()
        sendJoin()
        const stale = lk().onDisconnected
        apiConfig.data = loadedConfig('h264')
        view.rerender(
          <QueryClientProvider client={queryClient}>
            <Route path={routes.embedRoom.path}>
              <EmbedRoom />
            </Route>
          </QueryClientProvider>
        )
        expect(create).toHaveBeenCalledTimes(2) // the room was replaced
        return stale
      }

      it('says nothing for the disconnection of the replaced room, grace included', async () => {
        vi.useFakeTimers()
        const stale = replaceRoom()
        await act(async () => stale(DisconnectReason.CLIENT_INITIATED))
        await advance(DISCONNECT_GRACE_MS * 10)
        expect(posted().slice(1)).toEqual([])
      })

      it('says error unreachable, after the grace, for the disconnection of the current room', async () => {
        vi.useFakeTimers()
        replaceRoom()
        await bareDisconnect(undefined)
        await expectOnlyUnreachableAfterGrace()
      })
    })

    it('after joined, a disconnection says left alone, no error', () => {
      renderEmbed()
      sendJoin()
      act(() => lk().onConnected())
      act(() => lk().onDisconnected(DisconnectReason.SERVER_SHUTDOWN))
      expect(posted().slice(1)).toEqual([
        msg('joined'),
        msg('left', { reason: 'disconnected' }),
      ])
    })
  })
})
