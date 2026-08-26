import { useFeatureFlagEnabled } from 'posthog-js/react'
import { FeatureFlags } from '@/features/analytics/enums'
import { useIsAnalyticsEnabled } from '@/features/analytics/hooks/useIsAnalyticsEnabled'
import { useConfig } from '@/api/useConfig'
import { useLintoStatus } from '@/features/transcription-bot/hooks/useLintoStatus'

export const useAreSubtitlesAvailable = () => {
  const featureEnabled = useFeatureFlagEnabled(FeatureFlags.subtitles)
  const isAnalyticsEnabled = useIsAnalyticsEnabled()
  // LinTO live transcription (fork): the CC overlay renders the LinTO captions
  // too, so the button must be reachable while a LinTO run is active even when
  // the native subtitle agent is not deployed.
  const { active: isLintoActive } = useLintoStatus()

  const { data } = useConfig()

  return (
    isLintoActive ||
    (data?.subtitle.enabled && (!isAnalyticsEnabled || featureEnabled))
  )
}
