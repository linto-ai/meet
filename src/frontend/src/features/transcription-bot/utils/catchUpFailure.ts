import type { LintoCatchUpStatus } from '../store/lintoStore'

type CatchUpError = {
  code?: string
  status?: number
  message?: string
  reason?: string
}

// A missing relay route or a refused token hides the block ('unavailable'), so
// does a 429 (a reload within 20 s, nothing is wrong): 'idle'. Studio or its
// LLM failing (503) is an error the panel shows, with a retry.
export const catchUpFailureStatus = (err: unknown): LintoCatchUpStatus => {
  const { code, status } = (err ?? {}) as CatchUpError
  if (code === 'catchup_too_short') return 'too_short'
  if (code === 'catchup_rate_limited' || status === 429) return 'idle'
  if (status === 404 || code === 'catchup_forbidden') return 'unavailable'
  return 'error'
}

export const catchUpDisabledReason = (reason?: string): string =>
  reason || 'the Meet backend gave no reason'

// The cause, for the console: the panel only says the service is unavailable.
export const catchUpFailureReason = (err: unknown): string => {
  const { code, status, message, reason } = (err ?? {}) as CatchUpError
  if (status === 404) {
    return (
      'the Meet backend answered 404 on the catch-up relay: this Meet has no ' +
      'catch-up relay route (/rooms/:id/linto/catchup/), or LinTO is off'
    )
  }
  if (code === 'catchup_unavailable') {
    return `the Meet backend answered ${status ?? 503}: ${catchUpDisabledReason(reason)}`
  }
  if (code === 'catchup_forbidden') {
    return 'the Meet backend refused the LiveKit token of this room (403)'
  }
  if (code === 'catchup_rate_limited') {
    return 'the Meet backend allows one catch-up per participant every 20 s (429)'
  }
  return `${code ?? 'error'}${status ? ` (HTTP ${status})` : ''}: ${message ?? 'unknown error'}`
}
