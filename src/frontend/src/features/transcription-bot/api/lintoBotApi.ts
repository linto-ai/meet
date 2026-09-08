import {
  useMutation,
  UseMutationOptions,
  useQuery,
} from '@tanstack/react-query'
import { fetchApi } from '@/api/fetchApi'
import { ApiError } from '@/api/ApiError'
import type { LintoRuntimeConfig } from '@/api/useConfig'
import { LintoBotConfig, LintoBotProfilesResult } from '../types/linto'
import {
  StudioAuthUnavailable,
  StudioClient,
  useStudioClient,
} from './studioAuth'

// ── Browser-first data flow ──────────────────────────────────────────────────
// The panel drives LinTO Studio DIRECTLY via the JS SDK (authenticated as the
// user): it lists the quickMeeting profiles, creates the session, chooses the
// translations and starts/stops the bot. The Meet backend is only asked to do
// what the browser cannot: mint the native bot join token (`linto/prepare`) and
// run the room-wide lifecycle (`linto/started` / `linto/stopped`).

// Identifiers of a running LinTO transcription, kept so a later Stop can tear it
// down through the SDK.
export interface LintoRun {
  sessionId: string
  channelId: string
  botId: string | null
  organizationId: string
}

// The Studio session `meta` descriptor the native bot reads to join the room.
const nativeMeta = (
  livekitUrl: string,
  roomId: string,
  token?: string
): Record<string, unknown> => {
  const descriptor: Record<string, unknown> = { livekitUrl, room: roomId }
  if (token) descriptor.token = token
  return {
    native: { 'visio-native': descriptor },
    // One-release back-compat alias consumed by the Scheduler.
    linto_native: descriptor,
  }
}

// ── Profiles (SDK) ────────────────────────────────────────────────────────────

/**
 * quickMeeting ASR profiles for the user's organization, via the SDK. Same
 * `{ profiles, hasDefault, reason }` shape the panel/LintoSettings expect:
 * `reason='unprovisioned'` (org has none), `'ok'`, or `'upstream_error'` when
 * Studio is unreachable / the browser has no Studio auth yet.
 */
export function useLintoBotProfiles(
  roomId: string | undefined,
  token: string | undefined
) {
  const { getClient, config } = useStudioClient()
  return useQuery<LintoBotProfilesResult, ApiError>({
    queryKey: ['lintoBotProfiles', roomId],
    queryFn: async (): Promise<LintoBotProfilesResult> => {
      try {
        const { linto, organizationId } = await getClient(
          roomId as string,
          token as string
        )
        const profiles = await linto.listQuickMeetingProfiles({
          organizationId,
        })
        return {
          profiles,
          hasDefault: profiles.length > 0,
          reason: profiles.length > 0 ? 'ok' : 'unprovisioned',
        }
      } catch (err) {
        if (err instanceof StudioAuthUnavailable) {
          return { profiles: [], hasDefault: false, reason: 'upstream_error' }
        }
        // Any other failure (network / Studio down) degrades the same way.
        return { profiles: [], hasDefault: false, reason: 'upstream_error' }
      }
    },
    enabled: !!roomId && !!token && !!config?.enabled,
    staleTime: 5 * 60 * 1000,
  })
}

// ── Start / stop orchestration (SDK + Meet lifecycle hooks) ───────────────────

export interface StartLintoLiveParams {
  roomId: string
  token: string
  roomSlug: string
  config: LintoBotConfig
  lintoConfig: LintoRuntimeConfig
}

type GetClient = (roomId: string, token: string) => Promise<StudioClient>

const startLintoLive = async (
  { roomId, token, roomSlug, config, lintoConfig }: StartLintoLiveParams,
  getClient: GetClient
): Promise<LintoRun> => {
  const { linto, organizationId } = await getClient(roomId, token)

  // Resolve the ASR profile against what the org ACTUALLY has: use the ops-pinned
  // profile (config) only when it still exists, else fall back to the first
  // available one. A quickMeeting WITHOUT a valid profile is treated by Studio as
  // audio-only and rejects `keepAudio:false` (400), so a real profile is required.
  const profiles = await linto.listQuickMeetingProfiles({ organizationId })
  const pinned = lintoConfig.default_profile_id || config.asrProfileId
  const pinnedProfile = pinned
    ? profiles.find((p) => String(p.id) === String(pinned))
    : undefined
  const profileId = pinnedProfile?.id ?? profiles[0]?.id
  if (!profileId) {
    throw new StudioAuthUnavailable(
      'no quickMeeting ASR profile available for this organization'
    )
  }

  const channel = {
    name: 'Main',
    transcriberProfileId: profileId,
    enableLiveTranscripts: true,
    diarization: true,
    // Live transcription is text-only; the summary reads the finalized
    // conversation text, never stored audio.
    keepAudio: false,
    translations: config.translations,
  }

  const native = lintoConfig.visio_native_enabled
  // The public room URL the bot navigates to (SSRF-checked server-side).
  const botUrl = `${window.location.origin}/${roomSlug}`

  const run = await linto.launchVisioBot({
    organizationId,
    channel,
    // Declare the native descriptor up front (without the token); the token is
    // added by the metaWithToken hook once the channel id exists.
    meta: native
      ? nativeMeta(lintoConfig.native_livekit_url, roomId)
      : undefined,
    botUrl,
    provider: lintoConfig.bot_provider || 'visio',
    makePublic: true,
    // The native bot republishes the captions into the LiveKit room only when
    // asked to (an explicit `false` — the SDK's startBot default — opts it out);
    // the overlay and the panel are fed by exactly that republish.
    enableDisplaySub: true,
    metaWithToken: native
      ? async (_sessionId: string, channelId: string) => {
          // Mint the native bot join token via the Meet backend (needs Meet's
          // LiveKit secret) and inject it into the session meta.
          const prepared = await fetchApi<{ token: string }>(
            `rooms/${roomId}/linto/prepare/`,
            {
              method: 'POST',
              body: JSON.stringify({ channel_id: channelId, token }),
            }
          )
          return nativeMeta(
            lintoConfig.native_livekit_url,
            roomId,
            prepared.token
          )
        }
      : undefined,
  })

  // Record the run + light the room-wide state (banner, egress, summary hook).
  await fetchApi(`rooms/${roomId}/linto/started/`, {
    method: 'POST',
    body: JSON.stringify({
      session_id: run.sessionId,
      channel_id: run.channelId,
      org_id: run.organizationId ?? organizationId,
      bot_id: run.botId,
      summary: config.summary,
      record: config.record,
      token,
    }),
  })

  return {
    sessionId: run.sessionId,
    channelId: run.channelId,
    botId: run.botId,
    organizationId: run.organizationId ?? organizationId,
  }
}

export interface StopLintoLiveParams {
  roomId: string
  token: string
  run: Partial<LintoRun>
}

const stopLintoLive = async (
  { roomId, token, run }: StopLintoLiveParams,
  getClient: GetClient
): Promise<{ status: string }> => {
  const conversationName = `linto-${roomId}-${Math.floor(Date.now() / 1000)}`

  // Tear the run down through the SDK (best-effort) when we hold its ids.
  if (run.sessionId || run.botId) {
    try {
      const { linto } = await getClient(roomId, token)
      const organizationId = run.organizationId as string
      if (run.botId) {
        await linto
          .stopBot({ organizationId, botId: run.botId })
          .catch(() => {})
      }
      if (run.sessionId) {
        await linto
          .stopQuickMeeting({
            organizationId,
            sessionId: run.sessionId,
            name: conversationName,
          })
          .catch(() => {})
      }
    } catch {
      // Studio auth/teardown failure is non-fatal — the backend `stopped` hook
      // (and, on an abnormal exit, `room_finished` teardown) still clean up.
    }
  }

  // Clear the room-wide state + enqueue the summary (uses the conversation name).
  return fetchApi(`rooms/${roomId}/linto/stopped/`, {
    method: 'POST',
    body: JSON.stringify({ conversation_name: conversationName, token }),
  })
}

// ── react-query mutations ─────────────────────────────────────────────────────

export function useStartLintoLive(
  options?: UseMutationOptions<LintoRun, ApiError, StartLintoLiveParams>
) {
  const { getClient } = useStudioClient()
  return useMutation<LintoRun, ApiError, StartLintoLiveParams>({
    mutationFn: (params) => startLintoLive(params, getClient),
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
  const { getClient } = useStudioClient()
  return useMutation<{ status: string }, ApiError, StopLintoLiveParams>({
    mutationFn: (params) => stopLintoLive(params, getClient),
    ...options,
  })
}
