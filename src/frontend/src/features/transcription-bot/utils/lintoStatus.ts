import {
  LINTO_METADATA_CATCHUP_KEY,
  LINTO_METADATA_CHANNEL_ID_KEY,
  LINTO_METADATA_CHANNEL_INDEX_KEY,
  LINTO_METADATA_KEEP_AUDIO_KEY,
  LINTO_METADATA_ORG_ID_KEY,
  LINTO_METADATA_SESSION_ID_KEY,
  LINTO_METADATA_STARTED_AT_KEY,
  LINTO_METADATA_STARTER_KEY,
  LINTO_METADATA_STATUS_KEY,
} from '../types/linto'

export interface LintoStatus {
  // The feature is enabled AND a bot currently transcribes this room.
  active: boolean
  // Django id of the participant who started it ('' when anonymous/unknown).
  startedBy: string
  // Studio identity of the running session — the only way a LATE JOINER learns
  // what to fetch to catch up ('' when the backend published none).
  sessionId: string
  channelId: string
  channelIndex: number
  orgId: string
  // ISO 8601 UTC instant the run started ('' when unknown).
  startedAt: string
  // The starter left the "summary for late joiners" option on (default on
  // when the backend published nothing, for older runs).
  catchUpEnabled: boolean
  // The ACTIVE run keeps the meeting audio: every participant sees the
  // recording indicator. Off when the backend published nothing (a text-only
  // run, or one started before the key existed).
  keepsAudio: boolean
}

/**
 * Room-wide LinTO transcription state from the parsed LiveKit room metadata
 * (`undefined` when the room has none). Pure: shared by `useLintoStatus` and
 * the tests.
 */
export const lintoStatusFromMetadata = (
  enabled: boolean,
  metadata: Record<string, unknown> | undefined
): LintoStatus => {
  const active = enabled && metadata?.[LINTO_METADATA_STATUS_KEY] === 'active'
  return {
    active,
    startedBy: String(metadata?.[LINTO_METADATA_STARTER_KEY] ?? ''),
    sessionId: String(metadata?.[LINTO_METADATA_SESSION_ID_KEY] ?? ''),
    channelId: String(metadata?.[LINTO_METADATA_CHANNEL_ID_KEY] ?? ''),
    channelIndex: Number(metadata?.[LINTO_METADATA_CHANNEL_INDEX_KEY] ?? 0),
    orgId: String(metadata?.[LINTO_METADATA_ORG_ID_KEY] ?? ''),
    startedAt: String(metadata?.[LINTO_METADATA_STARTED_AT_KEY] ?? ''),
    catchUpEnabled:
      String(metadata?.[LINTO_METADATA_CATCHUP_KEY] ?? '1') !== '0',
    keepsAudio:
      active &&
      String(metadata?.[LINTO_METADATA_KEEP_AUDIO_KEY] ?? '0') === '1',
  }
}
