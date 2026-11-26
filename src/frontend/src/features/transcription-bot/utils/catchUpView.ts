import type { LintoCatchUpState } from '../store/lintoStore'
import {
  AUDIO_CATCH_UP_RUNNING,
  type AudioCatchUpBrief,
  type AudioCatchUpState,
} from '../store/audioCatchUpStore'

/** Where the "before you arrived" brief comes from: the captions of a live run
 *  of its own, or the audio of a transcribed recording. */
export type CatchUpSource = 'live' | 'audio'

/** What the brief's panel shows, whatever the source. */
export type CatchUpView = {
  source: CatchUpSource
  // The source's own status, exposed as `data-status`.
  status: string
  phase: 'preparing' | 'text' | 'too_short' | 'failed'
  // Audio only: the step being run (also while a previous brief shows), its
  // progress and the time left.
  step: 'sending' | 'transcribing' | 'summarizing' | null
  progress: number | null
  etaS: readonly [number, number] | null
  overdue: boolean
  text: string
  // Audio: the end of the meeting the brief covers. Live: my arrival.
  coveredUntil: number | null
  joinedAt: number | null
}

/** Joined at least `thresholdS` after the meeting's memory started. */
export const isLateArrival = (
  joinedAt: number,
  startedAt: number,
  thresholdS: number
): boolean =>
  Number.isFinite(joinedAt) &&
  Number.isFinite(startedAt) &&
  joinedAt > startedAt + thresholdS * 1000

// The audio state as a valtio snapshot hands it (deeply read-only).
type AudioInput = Readonly<
  Pick<AudioCatchUpState, 'status' | 'progress' | 'overdue' | 'dismissed'>
> & {
  readonly etaS: readonly [number, number] | null
  readonly brief: Readonly<AudioCatchUpBrief> | null
}

const base = {
  step: null,
  progress: null,
  etaS: null,
  overdue: false,
  text: '',
  coveredUntil: null,
  joinedAt: null,
} as const

/**
 * The view of the brief: from the live run when it serves the late joiners
 * (a run of its own whose catch-up option is on), else from the recording's
 * audio. Nothing to show (idle, closed, unavailable, failed) → null.
 */
export const catchUpView = ({
  liveServes,
  live,
  joinedAt,
  audio,
}: {
  liveServes: boolean
  live: Readonly<LintoCatchUpState>
  joinedAt: number | null
  audio: AudioInput
}): CatchUpView | null => {
  if (liveServes) {
    if (live.dismissed) return null
    const text = (live.text ?? '').trim()
    const view = {
      ...base,
      source: 'live' as const,
      status: live.status,
      joinedAt,
    }
    switch (live.status) {
      case 'loading':
        return { ...view, phase: 'preparing' }
      case 'streaming':
        return text
          ? { ...view, phase: 'text', text }
          : { ...view, phase: 'preparing' }
      case 'done':
        return text ? { ...view, phase: 'text', text } : null
      case 'too_short':
        return { ...view, phase: 'too_short' }
      default:
        return null
    }
  }
  if (audio.dismissed) return null
  const view = { ...base, source: 'audio' as const, status: audio.status }
  const brief = audio.brief
    ? {
        phase: 'text' as const,
        text: audio.brief.text,
        coveredUntil: audio.brief.coveredUntil,
      }
    : null
  if (AUDIO_CATCH_UP_RUNNING.includes(audio.status)) {
    return {
      ...view,
      phase: 'preparing',
      ...brief,
      step: audio.status as CatchUpView['step'],
      progress: audio.progress,
      etaS: audio.etaS,
      overdue: audio.overdue,
    }
  }
  if (audio.status === 'ready' && brief) return { ...view, ...brief }
  if (audio.status === 'failed') return { ...view, phase: 'failed', ...brief }
  if (audio.status === 'too_short') return { ...view, phase: 'too_short' }
  return null
}
