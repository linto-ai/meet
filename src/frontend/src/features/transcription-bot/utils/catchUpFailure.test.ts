import { describe, expect, it } from 'vitest'
import { catchUpFailureReason, catchUpFailureStatus } from './catchUpFailure'

// The errors the LinTO SDK throws on a catch-up call (catchUpError).
const sdkError = (status: number, code: string) =>
  Object.assign(new Error(`Request failed with status ${status}`), {
    status,
    code,
  })

describe('catchUpFailureStatus', () => {
  it('says the service is unavailable when Studio has no catch-up route', () => {
    expect(catchUpFailureStatus(sdkError(404, 'catchup_error'))).toBe(
      'unavailable'
    )
  })

  it('says the service is unavailable without an LLM service, or on a refusal', () => {
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

describe('catchUpFailureReason', () => {
  it('names the missing routes on a 404', () => {
    expect(catchUpFailureReason(sdkError(404, 'catchup_error'))).toContain(
      'no catch-up routes'
    )
  })

  it('names the gateway service on a 503', () => {
    expect(
      catchUpFailureReason(sdkError(503, 'catchup_unavailable'))
    ).toContain('LLM_CATCHUP_SERVICE')
  })

  it('carries the code and the HTTP status otherwise', () => {
    expect(catchUpFailureReason(sdkError(500, 'catchup_error'))).toBe(
      'catchup_error (HTTP 500): Request failed with status 500'
    )
    expect(catchUpFailureReason(new Error('network'))).toBe('error: network')
  })
})
