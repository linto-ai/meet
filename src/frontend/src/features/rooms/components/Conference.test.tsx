// @vitest-environment jsdom
import { type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render } from '@testing-library/react'
import { DisconnectReason } from 'livekit-client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { navigateTo } from '@/navigation/navigateTo'
import { reportError } from '@/features/analytics/telemetry'
import { Conference, type EmbedOptions } from './Conference'

/*
 * useLiveKitRoom reconnects when the onError (or onConnected, onDisconnected)
 * it is given changes identity: Conference must hand LiveKitRoom callbacks
 * that stay the same from one render to the next, whatever the embedding
 * recreates on each render, and that still reach the latest ones.
 */

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
    return <div>{props.children}</div>
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
  VideoConference: () => null,
}))
vi.mock('@/features/rooms/components/InviteDialog', () => ({
  InviteDialog: () => null,
}))
vi.mock('@/features/pip/components/PictureInPictureConference', () => ({
  PictureInPictureConference: () => null,
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
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock('@/utils/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('@/navigation/navigateTo', () => ({ navigateTo: vi.fn() }))
vi.mock('../api/joinRoom', () => ({
  joinRoom: vi.fn(() =>
    Promise.resolve({ id: 'r', livekit: { url: '', room: 'r', token: 't' } })
  ),
}))

const ROOM = '3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11'

const lk = () =>
  livekit.props as unknown as {
    onConnected: () => void
    onDisconnected: (reason?: DisconnectReason) => void
    onError: (error: Error) => void
  }

// A new embed (and new functions) on each call, as IntentService does
const freshEmbed = () => {
  const embed = {
    token: 'secret',
    audio: true,
    video: false,
    isLeaving: false,
    onConnected: vi.fn(),
    onDisconnected: vi.fn(),
    onError: vi.fn(),
  }
  return embed satisfies EmbedOptions
}

const tree = (embed?: EmbedOptions) => (
  <QueryClientProvider client={new QueryClient()}>
    <Conference roomId={ROOM} onLobbyRequired={() => {}} embed={embed} />
  </QueryClientProvider>
)

const renderAndLoad = async (embed?: EmbedOptions) => {
  const view = render(tree(embed))
  await vi.waitFor(() => expect(livekit.props.onError).toBeDefined())
  return view
}

describe('Conference callbacks given to LiveKitRoom', () => {
  beforeEach(() => {
    livekit.props = {}
    vi.mocked(navigateTo).mockReset()
    vi.mocked(reportError).mockReset()
  })
  afterEach(cleanup)

  describe('an ordinary call', () => {
    it('keeps the same onError, onConnected and onDisconnected across renders', async () => {
      const view = await renderAndLoad()
      const first = { ...lk() }
      view.rerender(tree())
      view.rerender(tree())
      expect(lk().onError).toBe(first.onError)
      expect(lk().onConnected).toBe(first.onConnected)
      expect(lk().onDisconnected).toBe(first.onDisconnected)
    })

    it('still reports an error as before', async () => {
      await renderAndLoad()
      const error = new Error('boom')
      lk().onError(error)
      expect(reportError).toHaveBeenCalledWith(
        'livekit_room_error',
        error,
        expect.anything()
      )
    })

    it('still remembers it was connected: a loss after it is a lost connection', async () => {
      await renderAndLoad()
      lk().onConnected()
      lk().onDisconnected(DisconnectReason.UNKNOWN_REASON)
      expect(navigateTo).toHaveBeenCalledWith(
        'feedback',
        {},
        expect.objectContaining({
          state: expect.objectContaining({ reason: 'connectionLost' }),
        })
      )
    })
  })

  describe('an embedded call whose embed is new on every render', () => {
    it('keeps the same onError, onConnected and onDisconnected across renders', async () => {
      const view = await renderAndLoad(freshEmbed())
      const first = { ...lk() }
      view.rerender(tree(freshEmbed()))
      view.rerender(tree(freshEmbed()))
      expect(lk().onError).toBe(first.onError)
      expect(lk().onConnected).toBe(first.onConnected)
      expect(lk().onDisconnected).toBe(first.onDisconnected)
    })

    it('calls the latest embed functions', async () => {
      const first = freshEmbed()
      const view = await renderAndLoad(first)
      const second = freshEmbed()
      view.rerender(tree(second))
      const third = freshEmbed()
      view.rerender(tree(third))

      lk().onConnected()
      lk().onDisconnected(DisconnectReason.PARTICIPANT_REMOVED)
      const error = new Error('boom')
      lk().onError(error)

      for (const old of [first, second]) {
        expect(old.onConnected).not.toHaveBeenCalled()
        expect(old.onDisconnected).not.toHaveBeenCalled()
        expect(old.onError).not.toHaveBeenCalled()
      }
      expect(third.onConnected).toHaveBeenCalledTimes(1)
      expect(third.onDisconnected).toHaveBeenCalledWith(
        DisconnectReason.PARTICIPANT_REMOVED
      )
      expect(third.onError).toHaveBeenCalledWith(error)
    })
  })
})
