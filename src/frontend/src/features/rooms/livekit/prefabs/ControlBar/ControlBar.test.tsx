// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ControlBar } from './ControlBar'

/*
 * The bar chosen follows the width of the viewport (the iframe in /embed):
 * the compact one, which moves what does not fit into the options menu, in a
 * small frame; the full one on a wide window.
 */

vi.mock('./MobileControlBar', () => ({
  MobileControlBar: () => <div data-testid="compact-control-bar" />,
}))
vi.mock('./DesktopControlBar', () => ({
  DesktopControlBar: () => <div data-testid="full-control-bar" />,
}))
vi.mock('@/features/reactions/components/toolbar/ReactionsToolbar', () => ({
  ReactionsToolbar: () => null,
}))

const DESKTOP_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36'

const setWidth = (width: number) => {
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(DESKTOP_UA)
  Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    writable: true,
    value: width,
  })
  window.matchMedia = ((query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query)
    const min = /min-width:\s*(\d+)px/.exec(query)
    return {
      matches:
        (!max || width <= Number(max[1])) && (!min || width >= Number(min[1])),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
    }
  }) as unknown as typeof window.matchMedia
}

describe('ControlBar', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('is the compact bar in a 420 px frame, desktop user agent or not', () => {
    setWidth(420)
    render(<ControlBar />)
    expect(screen.getByTestId('compact-control-bar')).toBeTruthy()
    expect(screen.queryByTestId('full-control-bar')).toBeNull()
  })

  it('is the full bar on a wide window', () => {
    setWidth(1280)
    render(<ControlBar />)
    expect(screen.getByTestId('full-control-bar')).toBeTruthy()
    expect(screen.queryByTestId('compact-control-bar')).toBeNull()
  })

  it.each([501, 550, 600])(
    'is the compact bar in a %i px frame: the full bar would overflow it',
    (width) => {
      setWidth(width)
      render(<ControlBar />)
      expect(screen.getByTestId('compact-control-bar')).toBeTruthy()
      expect(screen.queryByTestId('full-control-bar')).toBeNull()
    }
  )

  it('is the full bar from 601 px up', () => {
    setWidth(601)
    render(<ControlBar />)
    expect(screen.getByTestId('full-control-bar')).toBeTruthy()
    expect(screen.queryByTestId('compact-control-bar')).toBeNull()
  })
})
