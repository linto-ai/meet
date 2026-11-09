import { ApiError } from '@/api/ApiError'

export type LintoStartFailure =
  | { kind: 'already_running' }
  | { kind: 'already_starting' }
  | {
      kind: 'error'
      messageKey: 'errors.studioRefused' | 'errors.noProfile' | 'errors.generic'
    }

export const lintoStartFailure = (err: unknown): LintoStartFailure => {
  const body = err instanceof ApiError ? err.body : undefined
  const code =
    body && typeof body === 'object'
      ? (body as Record<string, unknown>).code
      : undefined
  if (code === 'already_running' || code === 'already_starting') {
    return { kind: code }
  }
  if (code === 'studio_refused') {
    return { kind: 'error', messageKey: 'errors.studioRefused' }
  }
  if (code === 'no_profile') {
    return { kind: 'error', messageKey: 'errors.noProfile' }
  }
  return { kind: 'error', messageKey: 'errors.generic' }
}
