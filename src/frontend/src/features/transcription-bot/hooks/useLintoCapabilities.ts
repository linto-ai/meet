import { useUser } from '@/features/auth/api/useUser'
import type { LintoCapabilities } from '../types/linto'

export interface LintoCapabilityState {
  // True while GET users/me is still loading (nothing decided yet).
  loading: boolean
  // The capabilities of the current user, null when nothing decides them
  // here (no LinTO on this instance, or a backend that knows nothing about
  // LinTO): no AI button at all. Guests have none.
  capabilities: LintoCapabilities | null
  live: boolean
  async: boolean
  summary: boolean
  translation: boolean
  recording: boolean
}

/**
 * The per-feature LinTO capabilities of the current participant, read from
 * GET users/me (`linto`, resolved by the backend's entitlements system at
 * login and cached). This is the ONE source the AI buttons are gated on: the
 * LinTO entry of the tools panel needs `transcription.live`, the "transcribe"
 * checkbox of the recording screen needs `transcription.async`, the recording
 * button needs `recording` when the instance gates it. Nothing here calls
 * Studio — the Studio token is only fetched when the user engages with the
 * LinTO panel.
 */
export const useLintoCapabilities = (): LintoCapabilityState => {
  const { user, isLoggedIn } = useUser()
  const loading = isLoggedIn === undefined
  const capabilities = user?.linto ?? null
  return {
    loading,
    capabilities,
    live: capabilities?.transcription?.live === true,
    async: capabilities?.transcription?.async === true,
    summary: capabilities?.summary === true,
    translation: capabilities?.translation === true,
    recording: capabilities?.recording === true,
  }
}
