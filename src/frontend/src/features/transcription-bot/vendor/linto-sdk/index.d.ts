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

export default class LinTO {
  constructor(options?: LintoOptions)
  apiService: {
    token: string | null
    organizations: Organization[]
    fetchOrganizations(args?: { token?: string }): Promise<Organization[]>
  }

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
    metaWithToken?: (
      sessionId: string,
      channelId: string
    ) => Promise<unknown> | unknown
  }): Promise<LaunchVisioBotResult>
}
