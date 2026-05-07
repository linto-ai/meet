import { proxy } from 'valtio'

export enum RecordingLanguage {
  ENGLISH = 'en',
  FRENCH = 'fr',
  DUTCH = 'nl',
  GERMAN = 'de',
  AUTOMATIC = 'auto',
}

type State = {
  language: RecordingLanguage
  isErrorDialogOpen: string
  // True when the local user started the active recording — drives toast
  // wording so non-starters don't see their own email as the recipient.
  startedByMe: boolean
}

export const recordingStore = proxy<State>({
  language: RecordingLanguage.FRENCH,
  isErrorDialogOpen: '',
  startedByMe: false,
})
