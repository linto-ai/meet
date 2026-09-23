import { proxy } from 'valtio'

export enum RecordingLanguage {
  ENGLISH = 'en',
  FRENCH = 'fr',
  DUTCH = 'nl',
  GERMAN = 'de',
  AUTOMATIC = 'auto',
}

// The deferred transcription tool's language when LinTO serves it: an ASR
// language code, or 'auto' (the service detects the spoken language).
export const LINTO_LANGUAGE_AUTO = 'auto'

type State = {
  language: RecordingLanguage
  isErrorDialogOpen: string
  // True when the local user started the active recording — drives toast
  // wording so non-starters don't see their own email as the recipient.
  startedByMe: boolean
  // "Transcribe after the meeting" choices when LinTO serves the transcription
  // (kept while the panel is closed and reopened): the language, whether a
  // summary is produced and with which LLM Gateway service.
  lintoLanguage: string
  lintoSummary: boolean
  lintoSummaryService?: string
}

export const recordingStore = proxy<State>({
  language: RecordingLanguage.FRENCH,
  isErrorDialogOpen: '',
  startedByMe: false,
  lintoLanguage: LINTO_LANGUAGE_AUTO,
  lintoSummary: true,
  lintoSummaryService: undefined,
})
