import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/ApiError'
import { lintoStartFailure } from './startFailure'

describe('lintoStartFailure', () => {
  it('reports nothing when the room already has a run', () => {
    expect(
      lintoStartFailure(new ApiError(409, { code: 'already_running' }))
    ).toEqual({ kind: 'already_running' })
    expect(
      lintoStartFailure(new ApiError(409, { code: 'already_starting' }))
    ).toEqual({ kind: 'already_starting' })
  })

  it('explains a refusal by Studio (402/403 at the creation)', () => {
    expect(
      lintoStartFailure(
        new ApiError(403, {
          code: 'studio_refused',
          studio_status: 402,
          error: 'LinTO refused to start the live transcription',
        })
      )
    ).toEqual({ kind: 'error', messageKey: 'errors.studioRefused' })
  })

  it('says when the organization has no profile', () => {
    expect(
      lintoStartFailure(new ApiError(409, { code: 'no_profile' }))
    ).toEqual({ kind: 'error', messageKey: 'errors.noProfile' })
  })

  it('never shows the backend text of any other failure', () => {
    expect(
      lintoStartFailure(
        new ApiError(502, { error: 'the live transcription could not start' })
      )
    ).toEqual({ kind: 'error', messageKey: 'errors.generic' })
    expect(
      lintoStartFailure(new ApiError(403, { code: 'permission_denied' }))
    ).toEqual({ kind: 'error', messageKey: 'errors.generic' })
    expect(lintoStartFailure(new Error('network'))).toEqual({
      kind: 'error',
      messageKey: 'errors.generic',
    })
  })
})
