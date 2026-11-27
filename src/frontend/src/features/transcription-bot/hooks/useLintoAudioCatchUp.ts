import { useEffect, useRef } from 'react'
import { useRoomContext } from '@livekit/components-react'
import { useSnapshot } from 'valtio'
import { useIsMobile } from '@/utils/useIsMobile'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useSidePanel } from '@/features/rooms/livekit/hooks/useSidePanel'
import { useRoomMetadata } from '@/features/recording/hooks/useRoomMetadata'
import { notifyLintoCatchUpReady } from '@/features/notifications'
import { ApiError } from '@/api/ApiError'
import {
  ApiAudioCatchUp,
  applyAudioCatchUp,
  getAudioCatchUp,
  requestAudioCatchUp,
} from '../api/audioCatchUpApi'
import {
  AUDIO_CATCH_UP_RUNNING,
  audioCatchUpStore,
  IDLE_AUDIO_CATCH_UP,
} from '../store/audioCatchUpStore'
import { useLiveServesCatchUp } from './useLiveServesCatchUp'

/**
 * "Before you arrived" from the audio of a TRANSCRIBED RECORDING, for a late
 * joiner when no live run of its own serves one (fork).
 *
 * The Meet backend decides whether the caller is late (its LiveKit join time
 * against the start of the audio capture), sends Studio the minutes it has
 * not sent yet and asks for the brief; this hook asks once per recording,
 * opens the side panel at arrival, then polls (each poll advances the job by
 * one step) until it ends. Participants who were there from the start get
 * `none` and see nothing.
 */

const POLL_INTERVAL_MS = 3000
const MAX_POLL_INTERVAL_MS = 30000
// Failed polls in a row before the panel says the connection is lost, then
// before it gives up (about four minutes of retries).
const RECONNECTING_AFTER = 2
const MAX_POLL_ERRORS = 10
// The arrival's request, again, when the backend could not be reached.
const ARRIVAL_RETRY_DELAYS_MS = [2000, 5000, 10000]
const RECORDING_RUNNING = ['starting', 'started']

const pollDelay = (errors: number) =>
  Math.min(POLL_INTERVAL_MS * 2 ** errors, MAX_POLL_INTERVAL_MS)

const wait = (ms: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, ms))

// No answer, or a server error: worth asking again. A 4xx (not in the room)
// is not.
const isRetryable = (err: unknown) =>
  !(err instanceof ApiError) || err.statusCode >= 500

/** sessionStorage key of this tab's first join time in a room. */
export const audioCatchUpJoinedAtKey = (roomId: string) =>
  `meet.linto.audioCatchup.joinedAt.${roomId}`

/**
 * The first time this tab joined the room, stable across reloads: LiveKit
 * gives a reloaded page a fresh join time, which would turn anyone into a late
 * joiner. The server takes the earliest of its own value and this one.
 */
const readFirstJoin = (roomId: string, fallback: number): number => {
  const key = audioCatchUpJoinedAtKey(roomId)
  try {
    const stored = Number(window.sessionStorage.getItem(key))
    if (Number.isFinite(stored) && stored > 0) return stored
    window.sessionStorage.setItem(key, String(fallback))
  } catch {
    // Storage disabled: the server's join time alone is used.
  }
  return fallback
}

// Shared by the hook's request and its polling. `generation` drops answers
// that belong to a superseded request.
const controller = {
  generation: 0,
  pollErrors: 0,
  roomId: '',
  token: '',
  isPanelOpen: () => false,
  // No side panel at all is open and the screen is not a phone's: the brief
  // may open the panel by itself.
  mayOpenPanel: () => false,
  openPanel: () => {},
}

const apply = (body: ApiAudioCatchUp, generation: number) => {
  if (generation !== controller.generation) return
  const previous = audioCatchUpStore.catchUp
  const next = applyAudioCatchUp(previous, body)
  audioCatchUpStore.catchUp = next
  if (
    next.status === 'ready' &&
    previous.status !== 'ready' &&
    !!next.brief &&
    !next.stale &&
    next.brief.text !== previous.brief?.text &&
    !controller.isPanelOpen()
  ) {
    notifyLintoCatchUpReady(() => controller.openPanel())
  }
}

/** Ask for the brief of what was said before my arrival, once per arrival.
 *  A late joiner gets the brief's panel opened by itself. A retry keeps the
 *  brief shown while it asks. */
export const requestAudioCatchUpBrief = async (
  roomId: string,
  token: string,
  { retry = false } = {}
) => {
  if (!roomId || !token) return
  const generation = ++controller.generation
  controller.pollErrors = 0
  const previous = audioCatchUpStore.catchUp
  audioCatchUpStore.catchUp = retry
    ? {
        ...IDLE_AUDIO_CATCH_UP,
        status: 'sending',
        recordingId: previous.recordingId,
        brief: previous.brief,
        updatedAt: Date.now(),
      }
    : { ...IDLE_AUDIO_CATCH_UP }
  for (let attempt = 0; ; attempt += 1) {
    const retryDelay = ARRIVAL_RETRY_DELAYS_MS[attempt]
    let body: ApiAudioCatchUp
    try {
      body = await requestAudioCatchUp(roomId, token, {
        joinedAtMs: readFirstJoin(roomId, Date.now()),
      })
    } catch (err) {
      if (generation !== controller.generation) return
      if (isRetryable(err) && retryDelay !== undefined) {
        await wait(retryDelay)
        if (generation !== controller.generation) return
        continue
      }
      // Not in the room, or the backend stayed out of reach.
      console.warn('LinTO audio catch-up request failed', err)
      audioCatchUpStore.catchUp = retry
        ? {
            ...audioCatchUpStore.catchUp,
            status: 'failed',
            stale: !!audioCatchUpStore.catchUp.brief,
            error: 'connection',
            updatedAt: Date.now(),
          }
        : {
            ...audioCatchUpStore.catchUp,
            status: 'unavailable',
            updatedAt: Date.now(),
          }
      return
    }
    if (
      body.state === 'unavailable' &&
      body.reason === 'linto_error' &&
      retryDelay !== undefined
    ) {
      await wait(retryDelay)
      if (generation !== controller.generation) return
      continue
    }
    if (retry && body.state === 'unavailable') {
      // Still out of service: the failure stays, with its reason.
      const current = audioCatchUpStore.catchUp
      audioCatchUpStore.catchUp = {
        ...current,
        status: 'failed',
        stale: !!current.brief,
        error: body.reason || current.error,
        updatedAt: Date.now(),
      }
      return
    }
    apply(body, generation)
    const { status } = audioCatchUpStore.catchUp
    if (status === 'unavailable') {
      console.warn(
        `LinTO audio catch-up unavailable: ${body.reason || 'unknown'}`
      )
    }
    if (
      !retry &&
      generation === controller.generation &&
      (AUDIO_CATCH_UP_RUNNING.includes(status) || status === 'ready') &&
      controller.mayOpenPanel()
    ) {
      controller.openPanel()
    }
    return
  }
}

/** "Retry" in the brief's panel: ask again, the server resumes or restarts. */
export const retryAudioCatchUp = () => {
  if (!controller.roomId || !controller.token) return
  void requestAudioCatchUpBrief(controller.roomId, controller.token, {
    retry: true,
  })
}

export const useLintoAudioCatchUp = () => {
  const room = useRoomContext()
  const apiRoomData = useRoomData()
  const metadata = useRoomMetadata()
  // A live run of its own that offers the catch-up serves the late joiners;
  // otherwise (live captions on top of a recording keep nothing) the
  // recording's brief does.
  const liveServes = useLiveServesCatchUp()
  const { catchUp } = useSnapshot(audioCatchUpStore)
  // The brief has its own panel. It opens by itself, never over another
  // panel nor on a phone.
  const { isCatchUpOpen, isSidePanelOpen, openCatchUp } = useSidePanel()
  const isMobile = useIsMobile()
  controller.isPanelOpen = () => isCatchUpOpen
  controller.mayOpenPanel = () => !isSidePanelOpen && !isMobile
  controller.openPanel = openCatchUp

  const roomId = apiRoomData?.livekit?.room || ''
  const token = apiRoomData?.livekit?.token || ''
  const tokenRef = useRef(token)
  tokenRef.current = token
  controller.roomId = roomId
  controller.token = token

  const recordingMode = metadata?.recording_mode as string | undefined
  const recordingRunning =
    !!recordingMode &&
    RECORDING_RUNNING.includes(String(metadata?.recording_status ?? ''))

  // Remember when this tab first joined, before any recording starts.
  useEffect(() => {
    if (!roomId) return
    readFirstJoin(
      roomId,
      room?.localParticipant?.joinedAt?.getTime() ?? Date.now()
    )
  }, [roomId, room])

  // One request per recording (the backend answers "none" to whoever was
  // there when the capture started), never while a live run has its own.
  const doneKeyRef = useRef<string | null>(null)
  useEffect(() => {
    if (!recordingRunning) {
      doneKeyRef.current = null
      return
    }
    if (liveServes) return
    if (!roomId || !token) return
    const key = `${roomId}:${recordingMode}`
    if (doneKeyRef.current === key) return
    doneKeyRef.current = key
    void requestAudioCatchUpBrief(roomId, token)
  }, [recordingRunning, recordingMode, liveServes, roomId, token])

  // Poll a running request every few seconds until it ends; failed polls
  // space out, the panel says so, and give up after a few minutes.
  const { status, recordingId, updatedAt } = catchUp
  useEffect(() => {
    if (!AUDIO_CATCH_UP_RUNNING.includes(status)) return
    if (!recordingId || !roomId) return
    const generation = controller.generation
    const timer = window.setTimeout(async () => {
      try {
        const body = await getAudioCatchUp(
          roomId,
          tokenRef.current,
          recordingId
        )
        controller.pollErrors = 0
        apply(body, generation)
      } catch {
        if (generation !== controller.generation) return
        controller.pollErrors += 1
        const current = audioCatchUpStore.catchUp
        audioCatchUpStore.catchUp =
          controller.pollErrors >= MAX_POLL_ERRORS
            ? {
                ...current,
                status: 'failed',
                stale: !!current.brief,
                reconnecting: false,
                error: 'connection',
                updatedAt: Date.now(),
              }
            : {
                ...current,
                reconnecting: controller.pollErrors >= RECONNECTING_AFTER,
                updatedAt: Date.now(),
              }
      }
    }, pollDelay(controller.pollErrors))
    return () => window.clearTimeout(timer)
  }, [status, recordingId, updatedAt, roomId])

  // Leaving the room drops any answer still in flight.
  useEffect(
    () => () => {
      controller.generation += 1
    },
    []
  )
}
