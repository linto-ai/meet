// @vitest-environment jsdom
import { type ReactNode } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import {
  ConnectionError,
  ConnectionErrorReason,
  DisconnectReason,
} from 'livekit-client'
import { Route } from 'wouter'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '@/api/queryClient'
import { fetchApi } from '@/api/fetchApi'
import { ApiError } from '@/api/ApiError'
import { navigateTo } from '@/navigation/navigateTo'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { routes } from '@/routes'
import { useEmbedIdleLeave } from '../EmbedIdleContext'
import IntentService from './IntentService'

/*
 * /intents?intent=<id>: Meet is the service of a cozy intent (cozy-interapp).
 * Same join / leave / idle / disconnect semantics as /embed/<uuid>, only the
 * transport changes: the host speaks through the service, never postMessage.
 * LiveKit and the in-call UI are stubs.
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
vi.mock('@/api/fetchApi', () => ({ fetchApi: vi.fn() }))
const interapp = vi.hoisted(() => ({
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
    createService = interapp.createService
  },
}))
// no i18n instance in tests: t(key) answers the key. The strings are checked
// in src/locales/embedIntent.test.ts
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
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

const ROOM = '3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11'
const OTHER_ROOM = '9a8b7c6d-1e2f-4a3b-8c4d-5e6f7a8b9c0d'
const TOKEN = 'eyJ.secret-livekit-token.sig'
const STACK = 'stack-access-token-secret'
const JOIN_TIMEOUT_MS = 10_000
const DISCONNECT_GRACE_MS = 1_000

const data = (fields: object = {}) => ({
  room: ROOM,
  token: TOKEN,
  audio: true,
  video: false,
  ...fields,
})
const sent = () => interapp.service.sendResult.mock.calls.map(([r]) => r)

const renderIntent = async () => {
  const view = render(
    <QueryClientProvider client={queryClient}>
      <Route path={routes.intentService.path}>
        <IntentService />
      </Route>
    </QueryClientProvider>
  )
  // stack-token, then the service, then its data
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  return view
}

class TestConnectionError extends ConnectionError {
  constructor(message: string, reason: ConnectionErrorReason, status?: number) {
    super(message, reason, status)
  }
}

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

const hostLeaves = () =>
  act(() => interapp.service.onData.mock.calls[0][0]({ leave: true }))

describe('/intents?intent=<id>', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    livekit.props = {}
    livekit.create = null
    apiConfig.data = loadedConfig()
    idle.leave = null
    livekit.room.disconnect = vi.fn(() => Promise.resolve())
    Object.values(interapp.service).forEach((fn) => fn.mockReset())
    interapp.service.getData.mockResolvedValue(data())
    interapp.createService.mockReset()
    interapp.createService.mockResolvedValue(interapp.service)
    vi.mocked(fetchApi).mockReset()
    vi.mocked(fetchApi).mockResolvedValue({
      access_token: STACK,
      platform_url: 'https://alice.twake.example.org',
    })
    window.history.pushState({}, '', '/intents?intent=intent-42')
  })

  afterEach(() => {
    cleanup()
    queryClient.clear()
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.mocked(navigateTo).mockClear()
    window.history.pushState({}, '', '/')
  })

  describe('before the data of the host', () => {
    it('shows no call and no join screen while the data is awaited', async () => {
      interapp.service.getData.mockReturnValue(new Promise(() => {}))
      await renderIntent()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(screen.queryByRole('button')).toBeNull()
      expect(interapp.service.sendResult).not.toHaveBeenCalled()
    })

    it('never joins when the service does not come in time', async () => {
      interapp.createService.mockReturnValue(new Promise(() => {}))
      await renderIntent()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS)
      })
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(interapp.service.getData).not.toHaveBeenCalled()
      expect(interapp.service.sendResult).not.toHaveBeenCalled()
    })

    it.each([
      ['a room that is not a uuid', { room: 'nope' }],
      ['no token', { token: '' }],
      ['audio that is not a boolean', { audio: 'yes' }],
    ])('says unsupported and shows no call for %s', async (_n, bad) => {
      interapp.service.getData.mockResolvedValue(data(bad))
      await renderIntent()
      expect(sent()).toEqual([{ event: 'error', code: 'unsupported' }])
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(interapp.service.notifyReadyToUse).not.toHaveBeenCalled()
    })

    it('never connects when the host leaves before the call', async () => {
      interapp.service.getData.mockReturnValue(new Promise(() => {}))
      await renderIntent()
      hostLeaves()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
    })
  })

  describe('joining with the data of the host', () => {
    it('connects LiveKit with its token on the server of its own /config/', async () => {
      await renderIntent()
      expect(screen.getByTestId('livekit-room')).toBeTruthy()
      expect(lk().token).toBe(TOKEN)
      expect(lk().serverUrl).toBe('wss://livekit.example.org')
    })

    it('says it is ready to use once, and the call is shown', async () => {
      await renderIntent()
      expect(interapp.service.notifyReadyToUse).toHaveBeenCalledTimes(1)
      expect(screen.getByTestId('video-conference')).toBeTruthy()
    })

    it('gives the call the room of the data (not of the URL) and the token, without asking the API for the room', async () => {
      interapp.service.getData.mockResolvedValue(data({ room: OTHER_ROOM }))
      await renderIntent()
      expect(screen.getByTestId('video-conference').textContent).toBe(
        `${OTHER_ROOM}|${TOKEN}`
      )
      const paths = vi.mocked(fetchApi).mock.calls.map(([p]) => String(p))
      expect(paths.filter((p) => p.includes('/rooms/'))).toEqual([])
    })

    it('takes microphone and camera from the data', async () => {
      interapp.service.getData.mockResolvedValue(
        data({ audio: false, video: true })
      )
      await renderIntent()
      expect(lk().audio).toBe(false)
      expect(lk().video).toBeTruthy()
    })

    it('shows no invitation dialog and no picture-in-picture', async () => {
      await renderIntent()
      expect(screen.queryByTestId('invite-dialog')).toBeNull()
      expect(screen.queryByTestId('picture-in-picture')).toBeNull()
    })

    it('keeps both tokens out of the URL and of every storage', async () => {
      await renderIntent()
      const everything = JSON.stringify([
        window.location.href,
        window.name,
        { ...window.localStorage },
        { ...window.sessionStorage },
      ])
      expect(everything).not.toContain(TOKEN)
      expect(everything).not.toContain(STACK)
    })

    it('says joined when LiveKit is connected, not before', async () => {
      await renderIntent()
      expect(sent()).toEqual([])
      act(() => lk().onConnected())
      expect(sent()).toEqual([{ event: 'joined' }])
      expect(interapp.service.terminate).not.toHaveBeenCalled()
    })
  })

  describe('leaving', () => {
    it('hangs up on leave from the host, says left (user) and terminates, once', async () => {
      await renderIntent()
      act(() => lk().onConnected())
      hostLeaves()
      expect(livekit.room.disconnect).toHaveBeenCalled()
      act(() => lk().onDisconnected(DisconnectReason.CLIENT_INITIATED))
      expect(sent().filter((r) => r.event === 'left')).toEqual([
        { event: 'left', reason: 'user' },
      ])
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })

    it('abandons a connection under way when the host leaves, and never says joined', async () => {
      await renderIntent()
      expect(lk().connect).toBe(true)
      hostLeaves()
      expect(lk().connect).toBe(false)
      act(() => lk().onConnected())
      expect(sent().map((r) => r.event)).not.toContain('joined')
    })

    it('says neither left nor error for the connection the host abandoned', async () => {
      await renderIntent()
      hostLeaves()
      act(() => lk().onConnected())
      act(() => lk().onDisconnected(DisconnectReason.CLIENT_INITIATED))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DISCONNECT_GRACE_MS * 10)
      })
      expect(sent()).toEqual([])
    })

    it('says left (idle) and shows nothing when idle, without /feedback', async () => {
      await renderIntent()
      act(() => lk().onConnected())
      act(() => idle.leave?.())
      expect(sent()).toEqual([
        { event: 'joined' },
        { event: 'left', reason: 'idle' },
      ])
      expect(navigateTo).not.toHaveBeenCalled()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })

    it.each([
      [DisconnectReason.CLIENT_INITIATED, 'user'],
      [DisconnectReason.PARTICIPANT_REMOVED, 'removed'],
      [DisconnectReason.SERVER_SHUTDOWN, 'disconnected'],
      [undefined, 'disconnected'],
    ])(
      'maps the LiveKit disconnect reason %s to left (%s)',
      async (why, reason) => {
        await renderIntent()
        act(() => lk().onConnected())
        act(() => lk().onDisconnected(why))
        expect(sent()).toEqual([{ event: 'joined' }, { event: 'left', reason }])
      }
    )

    it('shows nothing after the call, not even a feedback page', async () => {
      await renderIntent()
      act(() => lk().onConnected())
      act(() => lk().onDisconnected(DisconnectReason.CLIENT_INITIATED))
      expect(navigateTo).not.toHaveBeenCalled()
      expect(document.body.textContent).toBe('')
    })

    it('says left a single time when idle is followed by a disconnection', async () => {
      await renderIntent()
      act(() => lk().onConnected())
      act(() => idle.leave?.())
      act(() => lk().onDisconnected?.(DisconnectReason.CLIENT_INITIATED))
      expect(sent().filter((r) => r.event === 'left')).toHaveLength(1)
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })
  })

  describe('when the join fails', () => {
    it('says unknown for an error that is not a LiveKit connection error, then terminates', async () => {
      await renderIntent()
      act(() => lk().onError(new Error('boom')))
      expect(sent()).toEqual([{ event: 'error', code: 'unknown' }])
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })

    it('says nothing when the camera or microphone is refused', async () => {
      await renderIntent()
      act(() =>
        lk().onError(Object.assign(new Error('x'), { name: 'NotAllowedError' }))
      )
      expect(sent()).toEqual([])
    })

    it('says token_rejected when LiveKit refuses the token', async () => {
      await renderIntent()
      act(() =>
        lk().onError(
          new TestConnectionError('no', ConnectionErrorReason.NotAllowed, 401)
        )
      )
      expect(sent()).toEqual([{ event: 'error', code: 'token_rejected' }])
    })

    it('says unreachable when the LiveKit server cannot be reached', async () => {
      await renderIntent()
      act(() =>
        lk().onError(
          new TestConnectionError('no', ConnectionErrorReason.ServerUnreachable)
        )
      )
      expect(sent()).toEqual([{ event: 'error', code: 'unreachable' }])
    })

    it('says error alone, never left, when LiveKit disconnects after a refused token', async () => {
      await renderIntent()
      act(() =>
        lk().onError(
          new TestConnectionError('no', ConnectionErrorReason.NotAllowed, 401)
        )
      )
      act(() => lk().onDisconnected(DisconnectReason.JOIN_FAILURE))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DISCONNECT_GRACE_MS * 3)
      })
      expect(sent()).toEqual([{ event: 'error', code: 'token_rejected' }])
      expect(interapp.service.terminate).toHaveBeenCalledTimes(1)
    })

    it('says unreachable after the grace for a bare disconnection before joined', async () => {
      await renderIntent()
      act(() => lk().onDisconnected(undefined))
      expect(sent()).toEqual([])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DISCONNECT_GRACE_MS)
      })
      expect(sent()).toEqual([{ event: 'error', code: 'unreachable' }])
    })

    it('joined during the grace cancels it', async () => {
      await renderIntent()
      act(() => lk().onDisconnected(undefined))
      act(() => lk().onConnected())
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DISCONNECT_GRACE_MS * 3)
      })
      expect(sent()).toEqual([{ event: 'joined' }])
    })
  })

  describe('a session that is missing', () => {
    const popup = { closed: false, close: vi.fn() }
    const refuse401 = () =>
      vi
        .mocked(fetchApi)
        .mockRejectedValueOnce(new ApiError(401, {}))
        .mockResolvedValue({
          access_token: STACK,
          platform_url: 'https://alice.twake.example.org',
        })

    beforeEach(() => {
      popup.closed = false
      vi.spyOn(window, 'open').mockReturnValue(popup as never)
    })

    it('shows a sign-in button and opens no popup before the click', async () => {
      refuse401()
      await renderIntent()
      expect(
        screen.getByRole('button', { name: 'embed.intent.signIn' })
      ).toBeTruthy()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS)
      })
      expect(window.open).not.toHaveBeenCalled()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
      expect(interapp.createService).not.toHaveBeenCalled()
    })

    it('opens the popup on the click, then joins once the popup closed', async () => {
      refuse401()
      await renderIntent()
      fireEvent.click(
        screen.getByRole('button', { name: 'embed.intent.signIn' })
      )
      expect(window.open).toHaveBeenCalledTimes(1)
      popup.closed = true
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })
      expect(screen.getByTestId('livekit-room')).toBeTruthy()
      expect(
        screen.queryByRole('button', { name: 'embed.intent.signIn' })
      ).toBeNull()
    })

    it('says embed.intent.error.popup_blocked when the click opens no popup', async () => {
      refuse401()
      vi.mocked(window.open).mockReturnValue(null)
      await renderIntent()
      fireEvent.click(
        screen.getByRole('button', { name: 'embed.intent.signIn' })
      )
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(screen.getByText('embed.intent.error.popup_blocked')).toBeTruthy()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
    })

    it('shows the sign-in button on a 401 whose body is id_token_rejected', async () => {
      vi.mocked(fetchApi).mockRejectedValueOnce(
        new ApiError(401, { error: 'id_token_rejected' })
      )
      await renderIntent()
      expect(
        screen.getByRole('button', { name: 'embed.intent.signIn' })
      ).toBeTruthy()
      expect(screen.queryByRole('alert')).toBeNull()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
    })

    it('says embed.intent.error.unauthenticated when it is still 401 after the sign-in', async () => {
      vi.mocked(fetchApi).mockRejectedValue(new ApiError(401, {}))
      await renderIntent()
      fireEvent.click(
        screen.getByRole('button', { name: 'embed.intent.signIn' })
      )
      popup.closed = true
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })
      expect(
        screen.getByText('embed.intent.error.unauthenticated')
      ).toBeTruthy()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
    })
  })

  describe('an error before the service exists is shown by the page', () => {
    it.each([409, 400, 502])(
      'shows embed.intent.error.unreachable on a %s, with no call and no button',
      async (status) => {
        vi.mocked(fetchApi).mockRejectedValue(new ApiError(status, {}))
        await renderIntent()
        expect(screen.getByText('embed.intent.error.unreachable')).toBeTruthy()
        expect(screen.queryByTestId('livekit-room')).toBeNull()
        expect(screen.queryByRole('button')).toBeNull()
        expect(interapp.createService).not.toHaveBeenCalled()
      }
    )

    it('shows embed.intent.error.unreachable on a 502 stack_exchange_failed, with no button', async () => {
      vi.mocked(fetchApi).mockRejectedValue(
        new ApiError(502, { error: 'stack_exchange_failed' })
      )
      await renderIntent()
      expect(screen.getByText('embed.intent.error.unreachable')).toBeTruthy()
      expect(screen.queryByRole('button')).toBeNull()
    })

    it('shows embed.intent.error.unsupported when the service does not come in 10 s', async () => {
      interapp.createService.mockReturnValue(new Promise(() => {}))
      await renderIntent()
      expect(screen.queryByText('embed.intent.error.unsupported')).toBeNull()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS)
      })
      expect(screen.getByText('embed.intent.error.unsupported')).toBeTruthy()
      expect(screen.queryByTestId('livekit-room')).toBeNull()
    })

    it('puts no token and no intent id in the message', async () => {
      vi.mocked(fetchApi).mockRejectedValue(new ApiError(409, {}))
      await renderIntent()
      const text = document.body.textContent ?? ''
      expect(text).not.toContain(STACK)
      expect(text).not.toContain(TOKEN)
      expect(text).not.toContain('intent-42')
    })
  })

  it('does not speak to the host once unmounted', async () => {
    const view = await renderIntent()
    view.unmount()
    act(() => lk().onConnected())
    expect(sent()).toEqual([])
  })
})
