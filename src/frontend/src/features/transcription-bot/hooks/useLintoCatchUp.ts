import { useCallback, useEffect, useRef } from 'react'
import { useRoomContext } from '@livekit/components-react'
import { useSnapshot } from 'valtio'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useSidePanel } from '@/features/rooms/livekit/hooks/useSidePanel'
import { notifyLintoCatchUpReady } from '@/features/notifications'
import { useStudioClient } from '../api/studioAuth'
import { IDLE_CATCH_UP, lintoStore } from '../store/lintoStore'
import { hydrateCaptions } from '../store/transcriptStore'
import { LINTO_SEGMENT_PREFIX, LintoCaption } from '../types/linto'
import LinTO from '../vendor/linto-sdk'
import type { PublicSession } from '../vendor/linto-sdk'
import { useLintoStatus } from './useLintoStatus'

/**
 * "Catch-up" for a participant who joins while the transcription already runs.
 *
 * LiveKit never replays transcription segments, so a late joiner's panel starts
 * empty. This hook reads the Studio session identity from the room metadata
 * (published by the Meet backend at start), hydrates the transcript with the
 * finalized captions Studio already holds, and streams an LLM summary of
 * everything said BEFORE the local join time.
 *
 * It runs ONCE per (room, session), and never for the starter — who has seen
 * the whole meeting and needs neither the history nor the summary.
 */

// The transcript so far is fetched with the PUBLIC session route (the Meet flow
// PATCHes the session to `visibility: public` at start), so an anonymous guest
// with no Studio account can catch up too.

/** sessionStorage key holding the join time — see {@link readJoinedAt}. */
export const joinedAtStorageKey = (roomId: string, sessionId: string) =>
  `meet.linto.joinedAt.${roomId}.${sessionId}`

/**
 * The instant I joined the running transcription, stable across reloads.
 *
 * A page reload gives the local participant a FRESH `joinedAt`, which would
 * push the divider to "now" and re-summarize the whole meeting; the first value
 * seen for this (room, session) is therefore persisted for the tab's lifetime.
 */
const readJoinedAt = (
  roomId: string,
  sessionId: string,
  fallback: number
): number => {
  const key = joinedAtStorageKey(roomId, sessionId)
  try {
    const stored = window.sessionStorage.getItem(key)
    const parsed = stored ? Number(stored) : NaN
    if (Number.isFinite(parsed) && parsed > 0) return parsed
    window.sessionStorage.setItem(key, String(fallback))
  } catch {
    // Private mode / storage disabled: the in-memory value is good enough.
  }
  return fallback
}

type RawCaption = {
  segmentId?: string | number
  start?: number
  text?: string
  astart?: string
  lang?: string
  locutor?: string
}

type RawTranslation = {
  targetLang?: string
  lang?: string
  text?: string
}

/**
 * Map the finalized captions of one channel onto the store's line shape.
 *
 * The ids MUST match the ones the bot publishes live
 * (`linto:<sessionId>,<channelIndex>:<segmentId>`) so a line that arrives both
 * ways upserts instead of duplicating. `receivedAt` is the time the sentence was
 * actually SPOKEN (`astart` + relative `start`), which is what the journal
 * timestamps and the joined-at split rely on.
 */
export const mapSessionCaptions = (
  session: PublicSession,
  sessionId: string,
  channelIndex: number,
  joinedAt: number,
  baseTime: number
): LintoCaption[] => {
  const channels = session?.channels ?? []
  const channel =
    channels.find((c) => Number(c?.index) === channelIndex) ?? channels[0]
  if (!channel) return []

  const translationsBySegment = (channel.translatedCaptions ?? {}) as Record<
    string,
    RawTranslation[]
  >

  const captions = (channel.closedCaptions ?? []) as RawCaption[]
  const lines: LintoCaption[] = []
  for (const caption of captions) {
    if (caption?.segmentId === undefined || caption?.segmentId === null)
      continue
    const text = (caption.text ?? '').trim()
    if (!text) continue
    const segmentId = String(caption.segmentId)
    const absoluteStart = caption.astart ? Date.parse(caption.astart) : NaN
    const start = Number(caption.start) || 0
    const spokenAt =
      (Number.isFinite(absoluteStart) ? absoluteStart : baseTime) + start * 1000

    const translations: Record<string, string> = {}
    for (const translation of translationsBySegment[segmentId] ?? []) {
      const lang = translation?.targetLang || translation?.lang
      if (lang && translation?.text) translations[lang] = translation.text
    }

    lines.push({
      id: `${LINTO_SEGMENT_PREFIX}${sessionId},${channelIndex}:${segmentId}`,
      text,
      locutor: caption.locutor || '',
      language: caption.lang || undefined,
      startTime: start,
      partial: false,
      receivedAt: spokenAt,
      // Only what was said before I arrived is "history"; anything newer is a
      // line the live feed simply has not delivered to me yet.
      ...(spokenAt < joinedAt ? { catchup: true } : {}),
      ...(Object.keys(translations).length > 0 ? { translations } : {}),
    })
  }
  return lines.sort((a, b) => a.receivedAt - b.receivedAt)
}

/** The reader closed the "before you arrived" block: gone for this run. */
export const dismissLintoCatchUp = () => {
  lintoStore.catchUp = { ...lintoStore.catchUp, dismissed: true }
}

export const useLintoCatchUp = () => {
  const room = useRoomContext()
  const apiRoomData = useRoomData()
  const { getClient, config } = useStudioClient()
  const { active, sessionId, channelIndex, startedAt, catchUpEnabled } =
    useLintoStatus()
  const { startedByMe } = useSnapshot(lintoStore)
  // Read through refs: `useSidePanel` hands out new function identities on
  // every render, and the hydration effect below must NOT re-run (its cleanup
  // cancels the fetch in flight) for that.
  const { isLintoOpen, openLinto } = useSidePanel()
  const isLintoOpenRef = useRef(isLintoOpen)
  isLintoOpenRef.current = isLintoOpen
  const openLintoRef = useRef(openLinto)
  openLintoRef.current = openLinto

  const roomId = apiRoomData?.livekit?.room || room?.name || ''
  const roomToken = apiRoomData?.livekit?.token || ''

  // One run per (room, session): a metadata refresh or a re-render must not
  // re-hydrate the journal nor spend another LLM call.
  const doneKeyRef = useRef<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  /** The user's Studio client, or a bare one on the public path. */
  const resolveClient = useCallback(async (): Promise<
    InstanceType<typeof LinTO>
  > => {
    try {
      const { linto } = await getClient(roomId, roomToken)
      return linto
    } catch {
      // Anonymous guest (no SSO, no dev bridge): the public session routes need
      // no Studio auth, so an unauthenticated client is enough.
      return new LinTO({ baseUrl: config?.studio_api_url })
    }
  }, [getClient, roomId, roomToken, config?.studio_api_url])

  /** Stream the "before you arrived" summary into the store. It is computed
   *  ONCE per run: no refresh, and the block can be closed for good. When it
   *  lands while the panel is closed, a discreet toast offers to open it. */
  const runSummary = useCallback(
    async (
      linto: InstanceType<typeof LinTO>,
      session: string,
      index: number,
      joinedAt: number,
      token?: string
    ) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      lintoStore.catchUp = { ...IDLE_CATCH_UP, status: 'loading' }
      try {
        const { enabled } = await linto.catchUpStatus(session, { token })
        if (controller.signal.aborted) return
        if (!enabled) {
          // No LLM in this deployment — the panel hides the block entirely.
          lintoStore.catchUp = {
            ...IDLE_CATCH_UP,
            status: 'unavailable',
            updatedAt: Date.now(),
          }
          return
        }
        const { text } = await linto.catchUp(session, {
          token,
          before: new Date(joinedAt).toISOString(),
          channelIndex: index,
          signal: controller.signal,
          onToken: (_chunk, fullText) => {
            if (controller.signal.aborted) return
            lintoStore.catchUp = {
              status: 'streaming',
              text: fullText,
              updatedAt: Date.now(),
              dismissed: false,
            }
          },
        })
        if (controller.signal.aborted) return
        lintoStore.catchUp = {
          status: 'done',
          text,
          updatedAt: Date.now(),
          dismissed: false,
        }
        if (text.trim() && !isLintoOpenRef.current) {
          notifyLintoCatchUpReady(() => openLintoRef.current())
        }
      } catch (err) {
        if (controller.signal.aborted) return
        const code = (err as { code?: string })?.code
        const status =
          code === 'catchup_too_short'
            ? 'too_short'
            : code === 'catchup_unavailable' || code === 'catchup_forbidden'
              ? 'unavailable'
              : 'error'
        lintoStore.catchUp = {
          ...IDLE_CATCH_UP,
          status,
          updatedAt: Date.now(),
          error: code || (err as Error)?.message,
        }
      }
    },
    []
  )

  useEffect(() => {
    // The starter saw the whole meeting: never catch them up. Read the LIVE
    // store too, not just the render snapshot: on a starter's page reload,
    // `useSyncLintoStatus` (mounted just before this hook) promotes
    // `startedByMe` from the metadata in the SAME effect pass, so the snapshot
    // we rendered with is still false.
    if (!active || startedByMe || lintoStore.startedByMe) return
    if (!sessionId || !roomId || !config) return
    const key = `${roomId}:${sessionId}`
    if (doneKeyRef.current === key) return
    doneKeyRef.current = key

    const joinedAt = readJoinedAt(
      roomId,
      sessionId,
      room?.localParticipant?.joinedAt?.getTime() ?? Date.now()
    )
    lintoStore.joinedAt = joinedAt
    // Anchor for captions Session-API stored without an absolute start.
    const baseTime = Date.parse(startedAt || '') || joinedAt

    let cancelled = false
    const run = async () => {
      let linto: InstanceType<typeof LinTO>
      let session: PublicSession
      try {
        linto = await resolveClient()
        session = await linto.getPublicSession(sessionId)
      } catch (err) {
        console.warn('LinTO catch-up: cannot read the running session', err)
        // Allow a later retry (e.g. once the session is public).
        doneKeyRef.current = null
        return
      }
      if (cancelled) return
      hydrateCaptions(
        mapSessionCaptions(session, sessionId, channelIndex, joinedAt, baseTime)
      )
      // The summary is an option of the run: the history above is always
      // hydrated, the LLM is only asked when the starter left it on.
      if (!catchUpEnabled) return
      const token = session?.publicSessionToken
      await runSummary(linto, sessionId, channelIndex, joinedAt, token)
    }
    void run()

    return () => {
      cancelled = true
    }
  }, [
    active,
    startedByMe,
    sessionId,
    channelIndex,
    startedAt,
    catchUpEnabled,
    roomId,
    config,
    room,
    resolveClient,
    runSummary,
  ])

  // A finished run drops the summary and lets the next one catch up again.
  useEffect(() => {
    if (active) return
    doneKeyRef.current = null
    abortRef.current?.abort()
  }, [active])

  useEffect(
    () => () => {
      abortRef.current?.abort()
    },
    []
  )
}
