import { useLintoConfig } from '../hooks/useLintoConfig'
import { useLintoCatchUp } from '../hooks/useLintoCatchUp'
import { useLintoAudioCatchUp } from '../hooks/useLintoAudioCatchUp'
import { useCatchUpTooShortDismiss } from '../hooks/useCatchUpView'
import { useLintoTranscriptFeed } from '../hooks/useLintoTranscriptFeed'
import { useSyncLintoStatus } from '../hooks/useLintoStatus'
import { LintoBanner } from './LintoBanner'

const LintoRuntime = () => {
  // Room-wide state (metadata) → store, and LiveKit transcription segments →
  // transcript store. Both mounted once per room, for EVERY participant.
  useSyncLintoStatus()
  useLintoTranscriptFeed()
  // Late joiner only: hydrate the transcript so far + stream the "before you
  // arrived" summary. A no-op for the starter and outside a running session.
  useLintoCatchUp()
  // Late joiner of a transcribed recording with no live run: brief built from
  // the recording's audio by the Meet backend and LinTO.
  useLintoAudioCatchUp()
  useCatchUpTooShortDismiss()
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
