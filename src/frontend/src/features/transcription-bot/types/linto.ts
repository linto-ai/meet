// Public types for the LinTO live-transcription feature.
//
// The backend speaks snake_case (asr_profile_id, …); these camelCase shapes are
// the frontend-facing contract. Conversion happens at the API boundary in
// ../api/lintoBotApi.ts.

export interface LintoBotConfig {
  language?: string
  asrProfileId?: string
  // Live transcription is implicit (always on). `summary` adds an autonomous
  // summary at stop (default true); `record` adds a LiveKit video recording.
  summary: boolean
  record: boolean
  // Target translation language codes (0..N).
  translations: string[]
}

/**
 * One caption line as rendered by the panel / overlay.
 *
 * Live lines arrive as LiveKit transcription segments published by the LinTO
 * bot (ids `linto:<segmentId>`, translations `linto:<segmentId>:<lang>` merged
 * into `translations`); finalized lines can also be hydrated from bot-status.
 */
export interface LintoCaption {
  id: string
  text: string
  // Display name of the speaker (LiveKit participant name, or the Transcriber
  // locutor when the bot could not attribute the line).
  locutor: string
  // LiveKit identity of the speaker when known (drives avatar/colour).
  participantIdentity?: string
  language?: string
  // Relative start time (ms) when known — used to order hydrated lines.
  startTime?: number
  translations?: Record<string, string>
  // True while a turn is still in-flight (live partial); false once final.
  partial: boolean
  // Local receive time (ms epoch) — insertion order of live lines. Hydrated
  // history lines carry the time they were actually SPOKEN (astart + start).
  receivedAt: number
  // True for a line hydrated from Studio because it was said BEFORE I joined
  // (catch-up history): rendered dimmed, above the "you joined at" divider.
  catchup?: boolean
}

export interface LintoBotStatus {
  status: 'running' | 'idle' | 'stopped'
  sessionId?: string
  orgId?: string
  // Django id of the participant who started the bot; lets any participant
  // compute "started by me" / "may I stop".
  userId?: string
  summary?: boolean
  record?: boolean
  captions: LintoCaption[]
}

// A quickMeeting transcriber profile advertised by bot-profiles.
export interface LintoBotProfile {
  id: string
  name: string
  languages: string[]
  translations: string[]
}

// Why bot-profiles came back the way it did: 'ok' (have profiles or a default),
// 'unprovisioned' (org has none — admin must create one), 'upstream_error'.
export type LintoBotProfilesReason = 'ok' | 'unprovisioned' | 'upstream_error'

// Full bot-profiles result surfaced to the panel for the empty/error state.
export interface LintoBotProfilesResult {
  profiles: LintoBotProfile[]
  hasDefault: boolean
  reason: LintoBotProfilesReason
}

// Room-metadata keys written by the backend while a LinTO bot transcribes the
// room (the shared source of truth for every participant).
export const LINTO_METADATA_STATUS_KEY = 'linto_transcription_status'
export const LINTO_METADATA_STARTER_KEY = 'linto_transcription_started_by'

// Studio identity of the RUNNING session, published the same way so a LATE
// JOINER — who never saw the start and has no local session id — can fetch the
// transcript so far and the "before you arrived" summary (catch-up).
export const LINTO_METADATA_SESSION_ID_KEY = 'linto_transcription_session_id'
export const LINTO_METADATA_CHANNEL_ID_KEY = 'linto_transcription_channel_id'
export const LINTO_METADATA_CHANNEL_INDEX_KEY =
  'linto_transcription_channel_index'
export const LINTO_METADATA_ORG_ID_KEY = 'linto_transcription_org_id'
export const LINTO_METADATA_STARTED_AT_KEY = 'linto_transcription_started_at'

// Segment-id namespace used by the LinTO bot for the transcription segments it
// publishes into the LiveKit room.
export const LINTO_SEGMENT_PREFIX = 'linto:'
