import { proxy } from 'valtio'

// "Before you arrived" brief built from the recording's audio, for a late
// joiner when no live run of its own offers one. The Meet backend sends Studio
// the minutes it has not sent yet and asks for the summary; this store mirrors
// the state it answers.
export type AudioCatchUpStatus =
  | 'idle'
  | 'unavailable'
  | 'sending'
  | 'transcribing'
  | 'summarizing'
  | 'ready'
  | 'too_short'
  | 'failed'

export const AUDIO_CATCH_UP_RUNNING: readonly AudioCatchUpStatus[] = [
  'sending',
  'transcribing',
  'summarizing',
]

export type AudioCatchUpBrief = {
  // Markdown written by the LLM.
  text: string
  // Epoch ms of the end of the meeting the brief covers.
  coveredUntil: number | null
}

export type AudioCatchUpState = {
  status: AudioCatchUpStatus
  recordingId: string | null
  progress: number
  // [min, max] seconds still to wait, as estimated by the backend.
  etaS: [number, number] | null
  // The step runs past its estimate.
  overdue: boolean
  brief: AudioCatchUpBrief | null
  // The brief shown could not be updated (`error` says why).
  stale: boolean
  // Polls fail for now: the next ones keep trying.
  reconnecting: boolean
  // The reader closed the block: gone for this arrival.
  dismissed: boolean
  // Why the brief failed or was not updated, as the backend names it
  // (`summary_service`, `transcription`, `timeout`…), or `connection`/`stopped`.
  error?: string
  // Bumped on every server answer: drives the next poll.
  updatedAt: number
}

export const IDLE_AUDIO_CATCH_UP: AudioCatchUpState = {
  status: 'idle',
  recordingId: null,
  progress: 0,
  etaS: null,
  overdue: false,
  brief: null,
  stale: false,
  reconnecting: false,
  dismissed: false,
  error: undefined,
  updatedAt: 0,
}

export const audioCatchUpStore = proxy<{ catchUp: AudioCatchUpState }>({
  catchUp: { ...IDLE_AUDIO_CATCH_UP },
})

export const dismissAudioCatchUp = () => {
  audioCatchUpStore.catchUp = {
    ...audioCatchUpStore.catchUp,
    dismissed: true,
  }
}
