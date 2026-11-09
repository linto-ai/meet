import {
  useMutation,
  UseMutationOptions,
  useQuery,
} from '@tanstack/react-query'
import { fetchApi } from '@/api/fetchApi'
import { useConfig } from '@/api/useConfig'
import { getLiveKitAuthHeaders } from '@/features/rooms/utils/getLiveKitAuthHeaders'
import { ApiError } from '@/api/ApiError'
import {
  LintoBotConfig,
  LintoBotProfile,
  LintoBotProfilesReason,
  LintoBotProfilesResult,
  LintoSummaryService,
} from '../types/linto'
import { useLintoCapabilities } from '../hooks/useLintoCapabilities'

// The Meet backend drives LinTO Studio with its own credential; the panel only
// lists the profiles and asks for a start or a stop.

// ── Profiles (Meet backend) ───────────────────────────────────────────────────

// Wire shape of GET rooms/{id}/linto/profiles/.
interface LintoProfilesResponse {
  profiles?: LintoBotProfile[]
  reason?: LintoBotProfilesReason
}

/**
 * The live ASR profiles of the participant's organization, listed by the Meet
 * backend, with `reason` 'ok', 'unprovisioned', 'no_entitlement' or
 * 'upstream_error'. Only for a participant granted the live transcription.
 */
export function useLintoBotProfiles(
  roomId: string | undefined,
  token: string | undefined
) {
  const { data: apiConfig } = useConfig()
  const { live } = useLintoCapabilities()
  return useQuery<LintoBotProfilesResult, ApiError>({
    queryKey: ['lintoBotProfiles', roomId],
    queryFn: async (): Promise<LintoBotProfilesResult> => {
      try {
        const res = await fetchApi<LintoProfilesResponse>(
          `rooms/${roomId}/linto/profiles/`,
          { headers: getLiveKitAuthHeaders(token as string) }
        )
        const profiles = res.profiles ?? []
        return {
          profiles,
          hasDefault: profiles.length > 0,
          reason: res.reason ?? (profiles.length > 0 ? 'ok' : 'unprovisioned'),
        }
      } catch {
        return { profiles: [], hasDefault: false, reason: 'upstream_error' }
      }
    },
    enabled: !!roomId && !!token && !!apiConfig?.linto?.enabled && live,
    staleTime: 5 * 60 * 1000,
  })
}

// ── Summary services (Meet backend) ───────────────────────────────────────────

/**
 * The summary services the panel lets the user choose from: the LLM Gateway
 * services carrying the `meet` scope, listed by the Meet backend through
 * Studio for the organization the summaries are produced in. Empty when the
 * backend cannot answer (the panel then shows no choice).
 */
export function useLintoSummaryServices(
  roomId: string | undefined,
  token: string | undefined
) {
  const { data: apiConfig } = useConfig()
  // Both transcription tools offer the summary (live and deferred), to whoever
  // holds the summary capability.
  const { live, async: deferred, summary } = useLintoCapabilities()
  return useQuery<LintoSummaryService[], ApiError>({
    queryKey: ['lintoSummaryServices', roomId],
    queryFn: async () => {
      const res = await fetchApi<{ services?: LintoSummaryService[] }>(
        `rooms/${roomId}/linto/summary-services/`,
        { headers: getLiveKitAuthHeaders(token as string) }
      )
      return res.services ?? []
    },
    enabled:
      !!roomId &&
      !!token &&
      !!apiConfig?.linto?.enabled &&
      summary &&
      (live || deferred),
    staleTime: 5 * 60 * 1000,
  })
}

// ── Deferred transcription languages (Meet backend) ───────────────────────────

/**
 * The languages the "transcribe after the meeting" tool can be asked for:
 * what the gateway's STT services advertise, listed by the Meet backend
 * through Studio. `*` = automatic detection. Empty when the backend cannot
 * answer (the tool then offers the automatic detection only). Needs the
 * deferred-transcription capability, like the tool itself.
 */
export function useLintoTranscriptionLanguages(
  roomId: string | undefined,
  token: string | undefined
) {
  const { data: apiConfig } = useConfig()
  const { async: canTranscribeAsync } = useLintoCapabilities()
  return useQuery<string[], ApiError>({
    queryKey: ['lintoTranscriptionLanguages', roomId],
    queryFn: async () => {
      const res = await fetchApi<{ languages?: string[] }>(
        `rooms/${roomId}/linto/transcription-languages/`,
        { headers: getLiveKitAuthHeaders(token as string) }
      )
      return res.languages ?? []
    },
    enabled:
      !!roomId && !!token && !!apiConfig?.linto?.enabled && canTranscribeAsync,
    staleTime: 5 * 60 * 1000,
  })
}

// ── Start / stop (Meet backend) ───────────────────────────────────────────────

export interface LintoRunState {
  status: string
  session_id?: string
  user_id?: string | null
}

export interface StartLintoLiveParams {
  roomId: string
  token: string
  config: LintoBotConfig
}

const startLintoLive = ({
  roomId,
  token,
  config,
}: StartLintoLiveParams): Promise<LintoRunState> =>
  fetchApi<LintoRunState>(`rooms/${roomId}/linto/start/`, {
    method: 'POST',
    headers: getLiveKitAuthHeaders(token),
    body: JSON.stringify({
      translations: config.translations,
      summary: config.summary,
      summary_service: config.summary ? config.summaryService : undefined,
      record: config.record,
      catchup: config.catchup,
    }),
  })

export interface StopLintoLiveParams {
  roomId: string
  token: string
}

const stopLintoLive = ({
  roomId,
  token,
}: StopLintoLiveParams): Promise<{ status: string }> =>
  fetchApi<{ status: string }>(`rooms/${roomId}/linto/stop/`, {
    method: 'POST',
    headers: getLiveKitAuthHeaders(token),
  })

// ── react-query mutations ─────────────────────────────────────────────────────

export function useStartLintoLive(
  options?: UseMutationOptions<LintoRunState, ApiError, StartLintoLiveParams>
) {
  return useMutation<LintoRunState, ApiError, StartLintoLiveParams>({
    mutationFn: startLintoLive,
    ...options,
  })
}

export function useStopLintoLive(
  options?: UseMutationOptions<
    { status: string },
    ApiError,
    StopLintoLiveParams
  >
) {
  return useMutation<{ status: string }, ApiError, StopLintoLiveParams>({
    mutationFn: stopLintoLive,
    ...options,
  })
}
