import { useConfig } from '@/api/useConfig'

export interface LintoFeatureConfig {
  enabled: boolean
  // When true, the legacy "Transcrire" tool is hidden in favour of the LinTO one
  // (backend LINTO_HIDE_LEGACY_TOOLS).
  hideLegacyTools: boolean
}

/**
 * Reads the `linto` block of the runtime config (GET /api/v1.0/config/),
 * defaulting to disabled when the backend doesn't advertise the feature.
 */
export const useLintoConfig = (): LintoFeatureConfig => {
  const { data } = useConfig()
  return {
    enabled: data?.linto?.enabled ?? false,
    hideLegacyTools: data?.linto?.hide_legacy_tools ?? false,
  }
}
