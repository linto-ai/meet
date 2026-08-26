import {
  useMutation,
  UseMutationOptions,
  useQuery,
} from '@tanstack/react-query'
import { fetchApi } from '@/api/fetchApi'
import { ApiError } from '@/api/ApiError'
import {
  LintoBotConfig,
  LintoBotProfile,
  LintoBotProfilesReason,
  LintoBotProfilesResult,
  LintoBotStatus,
  LintoCaption,
} from '../types/linto'

// ── Wire (snake_case) shapes the backend actually speaks ──────────────────────

interface ApiBotConfig {
  language?: string
  asr_profile_id?: string
  summary: boolean
  record: boolean
  translations?: string[]
  token: string
}

interface ApiCaption {
  segment_id?: string | null
  text: string
  locutor: string
  participant_id?: string | null
  language?: string
  start?: number
  translations?: Record<string, string>
}

interface ApiBotStatusResponse {
  status: 'running' | 'idle' | 'stopped'
  session_id?: string
  org_id?: string
  user_id?: string
  summary?: boolean
  record?: boolean
  captions?: ApiCaption[]
}

interface ApiBotProfile {
  id: string
  name: string
  languages?: string[]
  translations?: string[]
}

interface ApiBotProfilesResponse {
  profiles?: ApiBotProfile[]
  hasDefault?: boolean
  reason?: LintoBotProfilesReason
}

// ── snake_case ↔ camelCase conversion at the boundary ─────────────────────────

const toCaption = (c: ApiCaption, index: number): LintoCaption => ({
  // Hydrated (finalized) lines share the bot's segment-id namespace so the live
  // feed and the history merge by id; a line without id gets a stable fallback.
  id: c.segment_id || `linto:hydrated:${index}`,
  text: c.text,
  locutor: c.locutor,
  participantIdentity: c.participant_id || undefined,
  language: c.language || undefined,
  startTime: typeof c.start === 'number' ? c.start * 1000 : undefined,
  translations: c.translations,
  partial: false,
  receivedAt: 0,
})

const toBotProfile = (p: ApiBotProfile): LintoBotProfile => ({
  id: p.id,
  name: p.name,
  languages: p.languages ?? [],
  translations: p.translations ?? [],
})

const toStatus = (res: ApiBotStatusResponse): LintoBotStatus => ({
  status: res.status,
  sessionId: res.session_id,
  orgId: res.org_id,
  userId: res.user_id,
  summary: res.summary,
  record: res.record,
  captions: (res.captions ?? []).map(toCaption),
})

const configToBody = (config: LintoBotConfig, token: string): ApiBotConfig => ({
  // Only forward set values — the backend has sensible defaults.
  ...(config.language && { language: config.language }),
  ...(config.asrProfileId && { asr_profile_id: config.asrProfileId }),
  ...(config.translations.length > 0 && { translations: config.translations }),
  summary: config.summary,
  record: config.record,
  // Same room-token-in-body mechanism as start-subtitle / recording:
  // LiveKitTokenAuthentication reads request.data["token"].
  token,
})

// ── Params ────────────────────────────────────────────────────────────────────

export interface StartLintoBotParams {
  roomId: string
  token: string
  config: LintoBotConfig
}

export interface StopLintoBotParams {
  roomId: string
  token: string
}

// ── Raw fetchers (calque startRecording.ts / startSubtitle.ts) ────────────────

const startLintoBot = async ({
  roomId,
  token,
  config,
}: StartLintoBotParams): Promise<LintoBotStatus> => {
  const res = await fetchApi<ApiBotStatusResponse>(
    `rooms/${roomId}/start-bot/`,
    {
      method: 'POST',
      body: JSON.stringify(configToBody(config, token)),
    }
  )
  return toStatus(res)
}

const stopLintoBot = ({
  roomId,
  token,
}: StopLintoBotParams): Promise<{ status: string }> => {
  return fetchApi(`rooms/${roomId}/stop-bot/`, {
    method: 'POST',
    body: JSON.stringify({ token }),
  })
}

const fetchLintoBotStatus = async (
  roomId: string,
  token: string
): Promise<LintoBotStatus> => {
  // bot-status is a GET; a browser fetch cannot carry a request body, so the
  // room token is passed as a query parameter (the POST siblings send it in the
  // body) — LiveKitTokenAuthentication reads request.query_params["token"].
  const search = token ? `?token=${encodeURIComponent(token)}` : ''
  const res = await fetchApi<ApiBotStatusResponse>(
    `rooms/${roomId}/bot-status/${search}`
  )
  return toStatus(res)
}

const fetchLintoBotProfiles = async (
  roomId: string,
  token: string
): Promise<LintoBotProfilesResult> => {
  const search = token ? `?token=${encodeURIComponent(token)}` : ''
  const res = await fetchApi<ApiBotProfilesResponse>(
    `rooms/${roomId}/bot-profiles/${search}`
  )
  return {
    profiles: (res.profiles ?? []).map(toBotProfile),
    hasDefault: res.hasDefault ?? false,
    reason: res.reason ?? 'ok',
  }
}

// ── react-query hooks ─────────────────────────────────────────────────────────

export function useStartLintoBot(
  options?: UseMutationOptions<LintoBotStatus, ApiError, StartLintoBotParams>
) {
  return useMutation<LintoBotStatus, ApiError, StartLintoBotParams>({
    mutationFn: startLintoBot,
    ...options,
  })
}

export function useStopLintoBot(
  options?: UseMutationOptions<{ status: string }, ApiError, StopLintoBotParams>
) {
  return useMutation<{ status: string }, ApiError, StopLintoBotParams>({
    mutationFn: stopLintoBot,
    ...options,
  })
}

/**
 * Bot status + finalized captions. Fetched ONCE when enabled (a participant
 * opening the panel while a transcription runs); the live feed itself comes
 * from LiveKit, and the running state from the room metadata.
 */
export function useLintoBotStatus(
  roomId: string | undefined,
  token: string | undefined,
  enabled: boolean
) {
  return useQuery<LintoBotStatus, ApiError>({
    queryKey: ['lintoBotStatus', roomId],
    queryFn: () => fetchLintoBotStatus(roomId as string, token as string),
    enabled: enabled && !!roomId && !!token,
    staleTime: 10 * 1000,
  })
}

/**
 * Fetches the quickMeeting transcriber profiles available to this room's
 * organization (bot-profiles). Populates the profile dropdown.
 */
export function useLintoBotProfiles(
  roomId: string | undefined,
  token: string | undefined
) {
  return useQuery<LintoBotProfilesResult, ApiError>({
    queryKey: ['lintoBotProfiles', roomId],
    queryFn: () => fetchLintoBotProfiles(roomId as string, token as string),
    enabled: !!roomId && !!token,
    staleTime: 5 * 60 * 1000,
  })
}
