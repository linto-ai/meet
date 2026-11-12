// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useIsMobile } from './useIsMobile'

/*
 * "Compact" layout (control bar with the overflow menu) must follow the width
 * of the viewport, i.e. of the iframe in /embed (detached call window
 * 420 x 340, phone band), not only the user agent: a desktop browser in a
 * 420 px frame is as narrow as a phone.
 */

const DESKTOP_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36'

const setWidth = (width: number) => {
  Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    writable: true,
    value: width,
  })
  // media queries answer from the same width
  window.matchMedia = ((query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query)
    const min = /min-width:\s*(\d+)px/.exec(query)
    const matches =
      (!max || width <= Number(max[1])) && (!min || width >= Number(min[1]))
    return {
      matches,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
    }
  }) as typeof window.matchMedia
}

describe('useIsMobile', () => {
  afterEach(() => vi.restoreAllMocks())

  it('is compact in a 420 px wide frame even with a desktop user agent', () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(DESKTOP_UA)
    setWidth(420)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it('is not compact on a wide desktop window', () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(DESKTOP_UA)
    setWidth(1280)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
  })

  it('follows the frame when it is resized', () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(DESKTOP_UA)
    setWidth(1280)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
    act(() => {
      setWidth(420)
      window.dispatchEvent(new Event('resize'))
    })
    expect(result.current).toBe(true)
  })
})
