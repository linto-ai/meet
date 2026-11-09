// What the "recording in progress" indicator (RecordingStateToast) shows to
// every participant. Two sources light it:
// - the LiveKit egress (deferred transcription, screen recording), through the
//   `recording_mode` / `recording_status` room metadata;
// - a LinTO live run that keeps the meeting audio, through the LinTO room
//   metadata (`linto_transcription_keep_audio`): its audio ends up in Twake
//   Drive, so participants see the same indicator as for a recording.
// When both are on, the egress wins: one indicator, never two.

// i18n key (rooms:recordingStateToast) of a LinTO live run keeping the audio.
export const LINTO_KEEP_AUDIO_INDICATOR_KEY = 'any.started'

export type RecordingIndicatorSource = 'egress' | 'linto'

export interface RecordingIndicatorInput {
  // Egress room metadata.
  recordingMode?: string
  recordingStatus?: string
  // useRecordingStatuses, either mode.
  isStarting: boolean
  isStarted: boolean
  // LiveKit's room.isRecording (the egress actually writes).
  isRecording: boolean
  // An active LinTO live run keeps the meeting audio.
  lintoKeepsAudio: boolean
}

export interface RecordingIndicator {
  // i18n key under rooms:recordingStateToast.
  key: string
  source: RecordingIndicatorSource
}

export const resolveRecordingIndicator = ({
  recordingMode,
  recordingStatus,
  isStarting,
  isStarted,
  isRecording,
  lintoKeepsAudio,
}: RecordingIndicatorInput): RecordingIndicator | undefined => {
  if (recordingMode && recordingStatus && (isStarting || isStarted)) {
    // Started in the metadata but LiveKit does not record yet: still starting.
    const status = isStarted && !isRecording ? 'starting' : recordingStatus
    return { key: `${recordingMode}.${status}`, source: 'egress' }
  }
  if (lintoKeepsAudio) {
    return { key: LINTO_KEEP_AUDIO_INDICATOR_KEY, source: 'linto' }
  }
  return undefined
}
