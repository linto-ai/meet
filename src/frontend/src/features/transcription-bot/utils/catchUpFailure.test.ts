import { describe, expect, it } from 'vitest'
import { LintoCatchUpError } from '../api/lintoBotApi'
import {
  catchUpDisabledReason,
  catchUpFailureReason,
  catchUpFailureStatus,
} from './catchUpFailure'

const relayError = (status: number, code: string, reason?: string) =>
  new LintoCatchUpError(
    `Request failed with status ${status}`,
    code,
    status,
    reason
  )

describe('catchUpFailureStatus', () => {
  it('says the service is unavailable when the Meet relay route is missing', () => {
    expect(catchUpFailureStatus(relayError(404, 'catchup_error'))).toBe(
      'unavailable'
    )
  })

  it('hides the block when the room token is refused', () => {
    expect(catchUpFailureStatus(relayError(403, 'catchup_forbidden'))).toBe(
      'unavailable'
    )
  })

  it('is an error the panel shows when Studio or its LLM fails', () => {
    expect(catchUpFailureStatus(relayError(503, 'catchup_unavailable'))).toBe(
      'error'
    )
  })

  it('says when too little was said', () => {
    expect(catchUpFailureStatus(relayError(204, 'catchup_too_short'))).toBe(
      'too_short'
    )
  })

  it('hides a rate limit: nothing is wrong with the service', () => {
    expect(catchUpFailureStatus(relayError(429, 'catchup_rate_limited'))).toBe(
      'idle'
    )
  })

  it('is an error otherwise', () => {
    expect(catchUpFailureStatus(relayError(500, 'catchup_error'))).toBe('error')
    expect(catchUpFailureStatus(new Error('network'))).toBe('error')
  })
})

describe('catchUpFailureReason', () => {
  it('names the missing relay route on a 404', () => {
    expect(catchUpFailureReason(relayError(404, 'catchup_error'))).toContain(
      'no catch-up relay route'
    )
  })

  it("carries the backend's reason on a 503", () => {
    expect(
      catchUpFailureReason(
        relayError(503, 'catchup_unavailable', 'no LLM Gateway service')
      )
    ).toBe('the Meet backend answered 503: no LLM Gateway service')
  })

  it('names the rate limit on a 429', () => {
    expect(
      catchUpFailureReason(relayError(429, 'catchup_rate_limited'))
    ).toContain('every 20 s')
  })

  it('carries the code and the HTTP status otherwise', () => {
    expect(catchUpFailureReason(relayError(500, 'catchup_error'))).toBe(
      'catchup_error (HTTP 500): Request failed with status 500'
    )
    expect(catchUpFailureReason(new Error('network'))).toBe('error: network')
  })
})

describe('catchUpDisabledReason', () => {
  it("is the backend's reason, or says it gave none", () => {
    expect(catchUpDisabledReason('catch-up off for this run')).toBe(
      'catch-up off for this run'
    )
    expect(catchUpDisabledReason()).toBe('the Meet backend gave no reason')
  })
})
