import { fetchApi } from '@/api/fetchApi'
import { getLiveKitAuthHeaders } from '@/features/rooms/utils/getLiveKitAuthHeaders'
import {
  AUDIO_CATCH_UP_RUNNING,
  AudioCatchUpBrief,
  AudioCatchUpState,
  AudioCatchUpStatus,
} from '../store/audioCatchUpStore'

// Room-scoped Meet backend route (LiveKit token in the Authorization header,
// like the other linto/* routes): POST starts or reuses the brief up to my
// arrival, GET advances the running job by one step and reads it. `live` means
// a live run of its own serves the late joiners instead.
export type ApiAudioCatchUp = {
  state:
    | 'none'
    | 'live'
    | 'unavailable'
    | 'sending'
    | 'transcribing'
    | 'summarizing'
    | 'ready'
    | 'too_short'
    | 'failed'
  reason?: string
  recording_id?: string
  progress?: number
  eta_s?: [number, number] | null
  overdue?: boolean
  // The brief could not be updated: `reason` says why.
  stale?: boolean
  joined_at?: string
  up_to?: string
  brief?: {
    text?: string
    covered_until?: string | null
  } | null
}

export const requestAudioCatchUp = (
  roomId: string,
  token: string,
  params: { joinedAtMs?: number }
) =>
  fetchApi<ApiAudioCatchUp>(`rooms/${roomId}/linto/audio-catchup/`, {
    method: 'POST',
    headers: getLiveKitAuthHeaders(token),
    body: JSON.stringify({ joined_at_ms: params.joinedAtMs }),
  })

export const getAudioCatchUp = (
  roomId: string,
  token: string,
  recordingId: string
) => {
  const query = new URLSearchParams({ recording_id: recordingId })
  return fetchApi<ApiAudioCatchUp>(
    `rooms/${roomId}/linto/audio-catchup/?${query.toString()}`,
    { headers: getLiveKitAuthHeaders(token) }
  )
}

const toMs = (value?: string | null): number | null => {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

const toBrief = (
  brief: ApiAudioCatchUp['brief'],
  previous: AudioCatchUpBrief | null
): AudioCatchUpBrief | null => {
  const text = brief?.text?.trim()
  if (!brief || !text) return previous
  return { text, coveredUntil: toMs(brief.covered_until) }
}

const toStatus = (state: ApiAudioCatchUp['state']): AudioCatchUpStatus =>
  state === 'none' || state === 'live' ? 'idle' : state

/** Merge a server answer into the store state (a missing field keeps its value). */
export const applyAudioCatchUp = (
  current: AudioCatchUpState,
  body: ApiAudioCatchUp
): AudioCatchUpState => {
  if (
    body.state === 'none' &&
    AUDIO_CATCH_UP_RUNNING.includes(current.status)
  ) {
    // The recording ended while its brief was being prepared.
    return {
      ...current,
      status: current.brief ? 'ready' : 'failed',
      stale: !!current.brief,
      reconnecting: false,
      etaS: null,
      overdue: false,
      error: 'stopped',
      updatedAt: Date.now(),
    }
  }
  const status = toStatus(body.state)
  return {
    ...current,
    status,
    recordingId: body.recording_id ?? current.recordingId,
    progress:
      typeof body.progress === 'number'
        ? Math.min(Math.max(body.progress, 0), 1)
        : status === 'ready'
          ? 1
          : current.progress,
    etaS: Array.isArray(body.eta_s) ? body.eta_s : null,
    overdue: !!body.overdue,
    brief: toBrief(body.brief, current.brief),
    stale: !!body.stale,
    reconnecting: false,
    error: body.reason || undefined,
    updatedAt: Date.now(),
  }
}
