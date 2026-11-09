import type { LintoCatchUpStatus } from '../store/lintoStore'

// A Studio without the catch-up routes answers 404: no summary, no error.
export const catchUpFailureStatus = (err: unknown): LintoCatchUpStatus => {
  const { code, status } = (err ?? {}) as { code?: string; status?: number }
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
