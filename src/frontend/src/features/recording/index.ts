// hooks
export { useIsRecordingModeEnabled } from './hooks/useIsRecordingModeEnabled'
export { useHasRecordingAccess } from './hooks/useHasRecordingAccess'
export { useHasFeatureWithoutRecordingRights } from './hooks/useHasFeatureWithoutRecordingRights'
export { useRecordingPermission } from './hooks/useRecordingPermission'
export { useRecordingStatuses } from './hooks/useRecordingStatuses'
export {
  useRecordingEncoding,
  DEFAULT_ENCODING_OPTION,
} from './hooks/useRecordingEncoding'
export type { RecordingEncoding } from './hooks/useRecordingEncoding'
export { useIsRecordingLocked } from './hooks/useIsRecordingLocked'

// api
export { useStartRecording } from './api/startRecording'
export { useStopRecording } from './api/stopRecording'
export { RecordingMode, RecordingPermission, RecordingStatus } from './types'

// components
export { RecordingProvider } from './components/RecordingProvider'
export { TranscriptSidePanel } from './components/TranscriptSidePanel'
export { ScreenRecordingSidePanel } from './components/ScreenRecordingSidePanel'
