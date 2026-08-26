import { useLintoConfig } from '../hooks/useLintoConfig'
import { useLintoTranscriptFeed } from '../hooks/useLintoTranscriptFeed'
import { useSyncLintoStatus } from '../hooks/useLintoStatus'
import { LintoBanner } from './LintoBanner'

const LintoRuntime = () => {
  // Room-wide state (metadata) → store, and LiveKit transcription segments →
  // transcript store. Both mounted once per room, for EVERY participant.
  useSyncLintoStatus()
  useLintoTranscriptFeed()
  return <LintoBanner />
}

/**
 * Ambient LinTO runtime for a room (calque of RecordingProvider): mounted in
 * VideoConference so the banner, the CC overlay feed and the shared state work
 * whether or not the panel is open. Renders nothing when the feature is off.
 */
export const LintoProvider = () => {
  const { enabled } = useLintoConfig()
  if (!enabled) return null
  return <LintoRuntime />
}
