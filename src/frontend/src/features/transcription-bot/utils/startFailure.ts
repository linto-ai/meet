import { ApiError } from '@/api/ApiError'

export type LintoStartFailure =
  | { kind: 'already_running' }
  | { kind: 'already_starting' }
  | {
      kind: 'error'
      messageKey:
        | 'errors.studioRefused'
        | 'errors.noProfile'
        | 'errors.tooManyTranslations'
        | 'errors.generic'
      params?: { count: number }
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
  if (code === 'too_many_translations') {
    const max = (body as Record<string, unknown>).max_translations
    return {
      kind: 'error',
      messageKey: 'errors.tooManyTranslations',
      params: { count: typeof max === 'number' ? max : 0 },
    }
  }
  return { kind: 'error', messageKey: 'errors.generic' }
}
