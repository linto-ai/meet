import type { LintoCatchUpStatus } from '../store/lintoStore'

type CatchUpError = {
  code?: string
  status?: number
  message?: string
  reason?: string
}

// A 429 hides the block (a reload within 20 s): nothing is wrong with the
// service, so it is not reported as unavailable.
export const catchUpFailureStatus = (err: unknown): LintoCatchUpStatus => {
  const { code, status } = (err ?? {}) as CatchUpError
  if (code === 'catchup_too_short') return 'too_short'
  if (
    status === 404 ||
    code === 'catchup_unavailable' ||
    code === 'catchup_forbidden'
  ) {
    return 'unavailable'
  }
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
