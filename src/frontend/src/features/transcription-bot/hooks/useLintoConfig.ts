import { useConfig } from '@/api/useConfig'

export interface LintoFeatureConfig {
  enabled: boolean
}

/**
 * Reads the `linto` block of the runtime config (GET /api/v1.0/config/),
 * defaulting to disabled when the backend doesn't advertise the feature.
 */
export const useLintoConfig = (): LintoFeatureConfig => {
  const { data } = useConfig()
  return {
    enabled: data?.linto?.enabled ?? false,
  }
}
