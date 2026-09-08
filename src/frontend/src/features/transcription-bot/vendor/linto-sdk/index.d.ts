// Minimal type declarations for the vendored LinTO Studio JS SDK
// (`@linto-ai/linto`). Only the surface the Meet frontend uses is typed; the
// runtime implementation is the sibling `index.js` (browser-native, fetch-based).

export interface LintoOptions {
  authToken?: string
  baseUrl?: string
}

export interface QuickMeetingProfile {
  id: string
  name: string
  languages: string[]
  translations: string[]
}

export interface QuickMeetingChannel {
  name: string
  transcriberProfileId?: string
  enableLiveTranscripts?: boolean
  diarization?: boolean
  keepAudio?: boolean
  translations?: string[]
}

export interface LaunchVisioBotResult {
  sessionId: string
  channelId: string
  botId: string | null
  organizationId?: string
}

export interface Organization {
  _id: string
  name?: string
}

// One finalized caption as Session-API persists it (catch-up history).
export interface PublicSessionCaption {
  segmentId?: string | number
  start?: number
  end?: number
  text?: string
  // Absolute wall-clock start of the AUDIO the segment belongs to (ISO 8601);
  // `astart + start` is the real time the sentence was spoken.
  astart?: string
  aend?: string
  lang?: string
  locutor?: string
}

export interface PublicSessionTranslation {
  segmentId?: string | number
  targetLang?: string
  lang?: string
  text?: string
}

export interface PublicSessionChannel {
  index?: number
  id?: string
  closedCaptions?: PublicSessionCaption[]
  translatedCaptions?: Record<string, PublicSessionTranslation[]>
}

export interface PublicSession {
  id?: string
  name?: string
  visibility?: string
  channels?: PublicSessionChannel[]
  // Bearer token authorizing the catch-up routes for an anonymous guest.
  publicSessionToken?: string
}

export interface CatchUpResult {
  text: string
  cached: boolean
}

// Machine-readable reason carried by a rejected catch-up call.
export type CatchUpErrorCode =
  | 'catchup_unavailable'
  | 'catchup_rate_limited'
  | 'catchup_too_short'
  | 'catchup_forbidden'

export interface CatchUpError extends Error {
  code?: CatchUpErrorCode | string
}

export default class LinTO {
  constructor(options?: LintoOptions)
  apiService: {
    token: string | null
    organizations: Organization[]
    fetchOrganizations(args?: { token?: string }): Promise<Organization[]>
  }

  /** LLM services available for summarization (empty = no LLM configured). */
  listLlmServices(): Promise<Array<Record<string, unknown>>>

  summarize(
    conversationId: string,
    serviceRoute: string,
    options?: { flavor?: string }
  ): Promise<unknown>

  // --- Catch-up (late joiner) ---

  getPublicSession(
    sessionId: string,
    options?: { token?: string }
  ): Promise<PublicSession>

  catchUpStatus(
    sessionId: string,
    options?: { token?: string }
  ): Promise<{ enabled: boolean }>

  catchUp(
    sessionId: string,
    options?: {
      token?: string
      before?: string
      channelIndex?: number
      maxChars?: number
      onToken?: (chunk: string, fullText: string) => void
      signal?: AbortSignal
    }
  ): Promise<CatchUpResult>

  listQuickMeetingProfiles(args?: {
    organizationId?: string
  }): Promise<QuickMeetingProfile[]>

  createQuickMeeting(args: {
    organizationId?: string
    channels: QuickMeetingChannel[]
    meta?: unknown
  }): Promise<{ id: string; channels: Array<{ id: string }> }>

  patchSession(args: {
    organizationId?: string
    sessionId: string
    data: Record<string, unknown>
  }): Promise<unknown>

  startBot(args: {
    organizationId?: string
    url: string
    channelId: string
    provider?: string
    enableDisplaySub?: boolean
    subSource?: string
  }): Promise<{ id: string }>

  stopBot(args: { organizationId?: string; botId: string }): Promise<unknown>

  stopQuickMeeting(args: {
    organizationId?: string
    sessionId: string
    name?: string
  }): Promise<unknown>

  findConversation(args: {
    organizationId?: string
    name?: string
    fromSessionId?: string
  }): Promise<string | null>

  launchVisioBot(args: {
    organizationId?: string
    channel: QuickMeetingChannel
    meta?: unknown
    botUrl: string
    provider?: string
    makePublic?: boolean
    /** Have the bot show the captions in the meeting (native visio bot: republish
     *  them into the LiveKit room, which feeds the overlay + panel). Default true. */
    enableDisplaySub?: boolean
    metaWithToken?: (
      sessionId: string,
      channelId: string
    ) => Promise<unknown> | unknown
  }): Promise<LaunchVisioBotResult>
}
