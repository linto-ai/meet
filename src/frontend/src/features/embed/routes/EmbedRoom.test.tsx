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
  createLiveKitRoom: () => livekit.room,
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
  useConfig: () => ({
    data: {
      auto_mute_on_join_threshold: 50,
      livekit: {
        url: 'wss://livekit.example.org',
        force_wss_protocol: false,
        enable_firefox_proxy_workaround: false,
        default_sources: [],
        default_video_codec: 'vp9',
      },
    },
  }),
}))
vi.mock('@/api/fetchApi', () => ({
  fetchApi: vi.fn(() => Promise.reject(new Error('no network in this test'))),
}))
vi.mock('@/navigation/navigateTo', () => ({ navigateTo: vi.fn() }))

const UUID = '3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11'
const CHAT = 'https://chat.example.org'
const TOKEN = 'eyJ.secret-livekit-token.sig'
const JOIN_TIMEOUT_MS = 10_000

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
    onConnected: () => void
    onDisconnected: (reason?: DisconnectReason) => void
    onError: (error: Error) => void
  }

describe('/embed/:uuid', () => {
  beforeEach(() => {
    parent.postMessage.mockReset()
    livekit.props = {}
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
})
