import { proxy } from 'valtio'

// Lifecycle of the "before you arrived" summary: 'unavailable' = the deployment
// has no LLM (block hidden), 'too_short' = nothing worth summarizing was said
// before I joined.
export type LintoCatchUpStatus =
  | 'idle'
  | 'loading'
  | 'streaming'
  | 'done'
  | 'error'
  | 'unavailable'
  | 'too_short'

export type LintoCatchUpState = {
  status: LintoCatchUpStatus
  // Markdown produced by the LLM, growing while it streams.
  text: string
  updatedAt: number | null
  error?: string
}

type LintoState = {
  // Mirrors the room-metadata "transcription in progress" flag (shared truth
  // for every participant) — see useLintoStatus. Also set optimistically by the
  // local start/stop mutations so the panel reacts before the metadata roundtrip.
  running: boolean
  // Identifiers of the running Studio session/bot (kept so a later Stop can tear
  // it down through the SDK). orgId doubles as the SDK organizationId.
  sessionId?: string
  channelId?: string
  botId?: string
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
  // Summary service route picked in the panel (undefined → instance default).
  summaryService?: string
  record: boolean
  selectedLanguage?: string
  // ASR profile id picked in the dropdown ("" / undefined → backend picks first).
  selectedProfile?: string
  // Target translation language codes (0..N).
  selectedTranslations: string[]
  // Shared 'displayed language' for the live transcript AND the caption overlay:
  // 'original' = the spoken language, else a translation target (base code).
  displayLanguage: string
  // Last start-bot error message (machine code mapped to a friendly string in
  // the panel), cleared on the next attempt.
  error?: string
  // Epoch ms at which I joined THIS room while the run was already going. Splits
  // the journal into catch-up history and live lines; null for the starter and
  // for anyone who was there before the transcription started.
  joinedAt: number | null
  // "Before you arrived" LLM summary of the pre-join transcript.
  catchUp: LintoCatchUpState
}

export const IDLE_CATCH_UP: LintoCatchUpState = {
  status: 'idle',
  text: '',
  updatedAt: null,
  error: undefined,
}

export const lintoStore = proxy<LintoState>({
  running: false,
  sessionId: undefined,
  channelId: undefined,
  botId: undefined,
  orgId: undefined,
  userId: undefined,
  startedByMe: false,
  localActionUntil: 0,
  summary: true,
  summaryService: undefined,
  record: false,
  selectedLanguage: undefined,
  selectedProfile: undefined,
  selectedTranslations: [],
  displayLanguage: 'original',
  error: undefined,
  joinedAt: null,
  catchUp: { ...IDLE_CATCH_UP },
})

export const resetLintoRun = () => {
  lintoStore.running = false
  lintoStore.sessionId = undefined
  lintoStore.channelId = undefined
  lintoStore.botId = undefined
  lintoStore.orgId = undefined
  lintoStore.userId = undefined
  // The catch-up belongs to the run that just ended: a next run starts from a
  // blank journal (clearTranscript) and must not show the previous summary.
  lintoStore.joinedAt = null
  lintoStore.catchUp = { ...IDLE_CATCH_UP }
  // `startedByMe` is intentionally kept (parity with recordingStore).
}
