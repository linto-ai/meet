import { fetchApi } from './fetchApi'
import { keys } from './queryKeys'
import { useQuery } from '@tanstack/react-query'
import { RecordingMode, RecordingPermission } from '@/features/recording'
import type { ApiAccessLevel } from '@/features/rooms/api/ApiRoom'
import type { Track } from 'livekit-client'
type Source = Track.Source

// Runtime config for the browser-first LinTO live transcription: the panel talks
// to studio-api DIRECTLY via the JS SDK, with a Studio token the Meet backend
// hands it (GET rooms/{id}/linto/studio-token — the identity bridge), and only
// mints the native bot join token + lifecycle hooks via the Meet backend.
export interface LintoRuntimeConfig {
  enabled: boolean
  hide_legacy_tools: boolean
  // Browser-facing studio-api base URL the LinTO JS SDK talks to.
  studio_api_url: string
  // Where the backend gets the Studio token it hands the browser (informational
  // — the panel always asks the bridge): the shared service account, or the
  // user's own LinTO API key via the studio-api identity exchange.
  token_source: 'service_account' | 'user_key'
  // Per-user feature gating (LINTO_ENTITLEMENTS_ENABLED). false = the kill
  // switch: every LinTO capability is granted to everyone, no entitlement lookup.
  entitlements_enabled: boolean
  // The video recording requires the `recording` capability
  // (LINTO_RECORDING_ENTITLEMENT_ENABLED); false = open to everyone.
  recording_entitlement_enabled: boolean
  // LiveKit signaling URL injected into the native bot descriptor.
  native_livekit_url: string
  visio_native_enabled: boolean
  bot_provider: string
  // Pinned ASR profile id (the panel never lets the user pick one); '' → the
  // first available profile is used.
  default_profile_id: string
}

export interface ApiConfig {
  analytics?: {
    id: string
    host: string
    flags_api_host?: string
  }
  support?: {
    id: string
    help_article_transcript: string
    help_article_recording: string
    help_article_more_tools: string
  }
  feedback: {
    url: string
  }
  documentation_url?: string
  external_home_url?: string
  silence_livekit_debug_logs?: boolean
  is_silent_login_enabled?: boolean
  custom_css_url?: string
  use_french_gov_footer?: boolean
  use_proconnect_button?: boolean
  idle_disconnect_warning_delay?: number
  recording?: {
    is_enabled?: boolean
    available_modes?: RecordingMode[]
    expiration_days?: number
    max_duration?: number
    screen_recording_permission?: RecordingPermission
    transcript_permission?: RecordingPermission
  }
  background_image: {
    upload_is_enabled: boolean
    max_size: number
    max_count_by_user: number
    allowed_extensions: string[]
    allowed_mimetypes: string[]
  }
  subtitle: {
    enabled: boolean
  }
  // LinTO live transcription (fork).
  linto?: LintoRuntimeConfig
  diagnostics: {
    connection_test_enabled?: boolean
  }
  telephony: {
    enabled: boolean
    international_phone_number?: string
    default_country?: string
  }
  resource?: {
    default_access_level?: ApiAccessLevel
  }
  manifest_link?: string
  livekit: {
    url: string
    force_wss_protocol: boolean
    enable_firefox_proxy_workaround: boolean
    default_sources: Source[]
  }
  transcription_destination?: string
  max_participants_for_sound: number
  auto_mute_on_join_threshold: number
  authenticated_users_can_edit_display_name: boolean
}

const fetchConfig = (): Promise<ApiConfig> => {
  return fetchApi<ApiConfig>(`config/`)
}

export const useConfig = () => {
  return useQuery({
    queryKey: [keys.config],
    queryFn: fetchConfig,
    staleTime: Infinity,
  })
}
