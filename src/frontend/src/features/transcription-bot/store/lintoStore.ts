import { proxy } from 'valtio'

type LintoState = {
  // Mirrors the room-metadata "transcription in progress" flag (shared truth
  // for every participant) — see useLintoStatus. Also set optimistically by the
  // local start/stop mutations so the panel reacts before the metadata roundtrip.
  running: boolean
  sessionId?: string
  orgId?: string
  // Django id of the participant who started the bot (room metadata / bot-status).
  userId?: string
  // True once I started the bot in this session. Set in the start onSuccess and
  // deliberately NOT reset on stop (mirrors recordingStore.startedByMe) so the
  // stop toast can address the summary email to my own address.
  startedByMe: boolean
  // Epoch ms until which the shared-state sync must NOT overwrite the store: a
  // guard against a stale bot-status / metadata roundtrip clobbering optimistic
  // local start/stop state.
  localActionUntil: number
  // Live transcription is ALWAYS on when LinTO runs (you clicked the tool). The
  // two flat options are independent add-ons: an autonomous summary at stop
  // (default ON) and a LiveKit video recording (default OFF).
  summary: boolean
  record: boolean
  selectedLanguage?: string
  // ASR profile id picked in the dropdown ("" / undefined → backend picks first).
  selectedProfile?: string
  // Target translation language codes (0..N).
  selectedTranslations: string[]
  // Last start-bot error message (machine code mapped to a friendly string in
  // the panel), cleared on the next attempt.
  error?: string
}

export const lintoStore = proxy<LintoState>({
  running: false,
  sessionId: undefined,
  orgId: undefined,
  userId: undefined,
  startedByMe: false,
  localActionUntil: 0,
  summary: true,
  record: false,
  selectedLanguage: undefined,
  selectedProfile: undefined,
  selectedTranslations: [],
  error: undefined,
})

export const resetLintoRun = () => {
  lintoStore.running = false
  lintoStore.sessionId = undefined
  lintoStore.orgId = undefined
  lintoStore.userId = undefined
  // `startedByMe` is intentionally kept (parity with recordingStore).
}
