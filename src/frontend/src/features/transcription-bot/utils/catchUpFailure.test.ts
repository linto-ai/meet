import { describe, expect, it } from 'vitest'
import { catchUpFailureStatus } from './catchUpFailure'

// The errors the LinTO SDK throws on a catch-up call (catchUpError).
const sdkError = (status: number, code: string) =>
  Object.assign(new Error(`Request failed with status ${status}`), {
    status,
    code,
  })

describe('catchUpFailureStatus', () => {
  it('hides the summary when Studio has no catch-up route', () => {
    expect(catchUpFailureStatus(sdkError(404, 'catchup_error'))).toBe(
      'unavailable'
    )
  })

  it('hides the summary when the deployment has no LLM or refuses it', () => {
    expect(catchUpFailureStatus(sdkError(503, 'catchup_unavailable'))).toBe(
      'unavailable'
    )
    expect(catchUpFailureStatus(sdkError(403, 'catchup_forbidden'))).toBe(
      'unavailable'
    )
  })

  it('says when too little was said', () => {
    expect(catchUpFailureStatus(sdkError(204, 'catchup_too_short'))).toBe(
      'too_short'
    )
  })

  it('is an error otherwise', () => {
    expect(catchUpFailureStatus(sdkError(500, 'catchup_error'))).toBe('error')
    expect(catchUpFailureStatus(new Error('network'))).toBe('error')
  })
})
