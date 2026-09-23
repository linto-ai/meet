// components
export { LintoSidePanel } from './components/LintoSidePanel'
export { LiveTranscript } from './components/LiveTranscript'
export { SummaryServicePicker } from './components/SummaryServicePicker'
export { TranslationPicker } from './components/TranslationPicker'
export { LintoBanner } from './components/LintoBanner'
export { LintoProvider } from './components/LintoProvider'

// hooks
export { useLintoConfig } from './hooks/useLintoConfig'
export { useLintoStatus } from './hooks/useLintoStatus'
export { useLintoEntitlement } from './hooks/useLintoEntitlement'
export { useLintoCapabilities } from './hooks/useLintoCapabilities'
export { useLintoTranscriptFeed } from './hooks/useLintoTranscriptFeed'

// api — browser-first: the panel drives Studio via the JS SDK; the Meet backend
// only mints the native token + runs the room-wide lifecycle hooks.
export {
  useLintoBotProfiles,
  useLintoSummaryServices,
  useLintoTranscriptionLanguages,
  useStartLintoLive,
  useStopLintoLive,
} from './api/lintoBotApi'
export { useStudioClient, StudioAuthUnavailable } from './api/studioAuth'

// stores
export { lintoStore } from './store/lintoStore'
export { transcriptStore, parseLintoSegmentId } from './store/transcriptStore'

// types
export type {
  LintoBotConfig,
  LintoCaption,
  LintoBotProfile,
  LintoBotProfilesReason,
  LintoBotProfilesResult,
  LintoCapabilities,
  LintoSummaryService,
} from './types/linto'
export { LINTO_SEGMENT_PREFIX } from './types/linto'
