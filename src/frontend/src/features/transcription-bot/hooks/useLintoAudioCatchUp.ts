import { useEffect, useRef } from 'react'
import { useRoomContext } from '@livekit/components-react'
import { useSnapshot } from 'valtio'
import { useIsMobile } from '@/utils/useIsMobile'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useSidePanel } from '@/features/rooms/livekit/hooks/useSidePanel'
import { useRoomMetadata } from '@/features/recording/hooks/useRoomMetadata'
import { notifyLintoCatchUpReady } from '@/features/notifications'
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
const MAX_POLL_ERRORS = 5
const RECORDING_RUNNING = ['starting', 'started']

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
    !controller.isPanelOpen()
  ) {
    notifyLintoCatchUpReady(() => controller.openPanel())
  }
}

/** Ask for the brief of what was said before my arrival, once per arrival.
 *  A late joiner gets the brief's panel opened by itself. */
export const requestAudioCatchUpBrief = async (
  roomId: string,
  token: string
) => {
  if (!roomId || !token) return
  const generation = ++controller.generation
  controller.pollErrors = 0
  audioCatchUpStore.catchUp = { ...IDLE_AUDIO_CATCH_UP }
  try {
    const body = await requestAudioCatchUp(roomId, token, {
      joinedAtMs: readFirstJoin(roomId, Date.now()),
    })
    apply(body, generation)
    const { status } = audioCatchUpStore.catchUp
    if (status === 'unavailable') {
      console.warn(
        `LinTO audio catch-up unavailable: ${body.reason || 'unknown'}`
      )
    }
    if (
      generation === controller.generation &&
      (AUDIO_CATCH_UP_RUNNING.includes(status) || status === 'ready') &&
      controller.mayOpenPanel()
    ) {
      controller.openPanel()
    }
  } catch {
    if (generation !== controller.generation) return
    // Not in the room, backend down…: nothing to show.
    audioCatchUpStore.catchUp = {
      ...audioCatchUpStore.catchUp,
      status: 'unavailable',
      updatedAt: Date.now(),
    }
  }
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

  // Poll a running request every few seconds until it ends.
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
        const failed = controller.pollErrors >= MAX_POLL_ERRORS
        audioCatchUpStore.catchUp = {
          ...audioCatchUpStore.catchUp,
          ...(failed ? { status: 'failed', error: 'poll_failed' } : {}),
          updatedAt: Date.now(),
        }
      }
    }, POLL_INTERVAL_MS)
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
