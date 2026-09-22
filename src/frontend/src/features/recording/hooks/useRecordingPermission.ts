import { useFeatureFlagEnabled } from 'posthog-js/react'
import { useIsAnalyticsEnabled } from '@/features/analytics/hooks/useIsAnalyticsEnabled'
import { RecordingMode, RecordingPermission } from '../types'
import { useIsRecordingModeEnabled } from './useIsRecordingModeEnabled'
import { useIsAdminOrOwner } from '@/features/rooms/livekit/hooks/useIsAdminOrOwner'
import { FeatureFlags } from '@/features/analytics/enums'
import { useConfig } from '@/api/useConfig'
import { useUser } from '@/features/auth/api/useUser'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useLintoCapabilities } from '@/features/transcription-bot/hooks/useLintoCapabilities'

/**
 * Internal hook that computes recording permission state for a given mode.
 * Used by useHasRecordingAccess and useHasFeatureWithoutAdminRights.
 */
export const useRecordingPermission = (
  mode: RecordingMode,
  featureFlag: FeatureFlags
) => {
  const featureEnabled = useFeatureFlagEnabled(featureFlag)
  const isAnalyticsEnabled = useIsAnalyticsEnabled()
  const isRecordingModeEnabled = useIsRecordingModeEnabled(mode)
  const isAdminOrOwner = useIsAdminOrOwner()
  const { data: config } = useConfig()
  const { isLoggedIn } = useUser()
  const roomData = useRoomData()

  const permissionLevel =
    mode === RecordingMode.ScreenRecording
      ? (roomData?.recording_permissions?.screen_recording_permission ??
        config?.recording?.screen_recording_permission)
      : (roomData?.recording_permissions?.transcript_permission ??
        config?.recording?.transcript_permission)

  const hasPermission =
    permissionLevel === RecordingPermission.Authenticated
      ? isLoggedIn
      : isAdminOrOwner

  // The video recording may be reserved to a plan (LinTO fork): when the
  // instance gates it, the participant needs the `recording` capability. It
  // closes the FEATURE (not the organizer permission), so the panel shows the
  // "advanced feature" view rather than "ask an administrator".
  const { recording: recordingEntitled } = useLintoCapabilities()
  const recordingGated = config?.linto?.recording_entitlement_enabled === true
  const entitled =
    mode !== RecordingMode.ScreenRecording ||
    !recordingGated ||
    recordingEntitled

  const isFeatureAvailable =
    (featureEnabled || !isAnalyticsEnabled) &&
    isRecordingModeEnabled &&
    entitled

  return { isFeatureAvailable, hasPermission }
}
