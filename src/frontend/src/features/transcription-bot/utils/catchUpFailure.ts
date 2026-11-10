import type { LintoCatchUpStatus } from '../store/lintoStore'

type CatchUpError = { code?: string; status?: number; message?: string }

export const CATCH_UP_NO_SERVICE =
  'Studio resolves no LLM Gateway service for the catch-up ' +
  '(LLM_CATCHUP_SERVICE, default visio-catchup, then LLM_CHAT_SERVICE_ID)'

// A Studio without the catch-up routes answers 404.
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

// The cause, for the console: the panel only says the service is unavailable.
export const catchUpFailureReason = (err: unknown): string => {
  const { code, status, message } = (err ?? {}) as CatchUpError
  if (status === 404) {
    return (
      'Studio answered 404 on the catch-up route: this Studio has no ' +
      'catch-up routes (/api/sessions/public/:id/catchup), or it does not ' +
      'know the session'
    )
  }
  if (code === 'catchup_unavailable') {
    return (
      `Studio answered 503: ${CATCH_UP_NO_SERVICE}, or the LLM Gateway is ` +
      'unreachable or refused the completion'
    )
  }
  if (code === 'catchup_forbidden') {
    return 'Studio refused the public session token (403)'
  }
  return `${code ?? 'error'}${status ? ` (HTTP ${status})` : ''}: ${message ?? 'unknown error'}`
}
