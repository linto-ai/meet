// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import IntentSignedIn from './IntentSignedIn'

/*
 * /intents/signed-in: where the sign-in popup of the intent page lands. It
 * tells the page that opened it, on our own origin only, and closes itself.
 */

const opener = { postMessage: vi.fn() }

describe('/intents/signed-in', () => {
  beforeEach(() => {
    opener.postMessage.mockReset()
    Object.defineProperty(window, 'opener', {
      configurable: true,
      value: opener,
    })
    vi.spyOn(window, 'close').mockImplementation(() => {})
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'opener')
  })

  it('tells the opener it is signed in, for our own origin only', () => {
    render(<IntentSignedIn />)
    expect(opener.postMessage).toHaveBeenCalledTimes(1)
    expect(opener.postMessage).toHaveBeenCalledWith(
      { source: 'twake-meet-intent', type: 'signed-in' },
      window.location.origin
    )
  })

  it('never uses a wildcard target origin', () => {
    render(<IntentSignedIn />)
    expect(opener.postMessage.mock.calls[0][1]).not.toBe('*')
  })

  it('closes itself', () => {
    render(<IntentSignedIn />)
    expect(window.close).toHaveBeenCalled()
  })

  it('posts and closes only once when rendered again', () => {
    const { rerender } = render(<IntentSignedIn />)
    rerender(<IntentSignedIn />)
    expect(opener.postMessage).toHaveBeenCalledTimes(1)
  })

  it('still closes, and does not throw, without an opener', () => {
    Object.defineProperty(window, 'opener', {
      configurable: true,
      value: null,
    })
    expect(() => render(<IntentSignedIn />)).not.toThrow()
    expect(window.close).toHaveBeenCalled()
  })
})
