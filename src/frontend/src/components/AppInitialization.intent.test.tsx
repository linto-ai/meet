// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppInitialization } from './AppInitialization'

const bridge = vi.hoisted(() => ({ created: vi.fn() }))

vi.mock('cozy-external-bridge', () => ({
  CozyBridge: class {
    constructor() {
      bridge.created()
    }
    isInIframe = () => false
    requestParentOrigin = () => Promise.resolve(null)
    setupBridge = () => {}
    startHistorySyncing = () => {}
  },
}))
vi.mock('@/api/useConfig', () => ({ useConfig: () => ({ data: {} }) }))
vi.mock('@/features/analytics/hooks/useAnalytics', () => ({
  useAnalytics: () => {},
}))
vi.mock('@/features/auth/api/useSyncUserPreferencesWithBackend', () => ({
  useSyncUserPreferencesWithBackend: () => {},
}))

describe('cozy bridge', () => {
  afterEach(() => {
    cleanup()
    bridge.created.mockClear()
    window.history.pushState({}, '', '/')
  })

  it('is set up on an ordinary page', () => {
    window.history.pushState({}, '', '/abc-defg-hij')
    render(<AppInitialization />)
    expect(bridge.created).toHaveBeenCalled()
  })

  it('is not mounted under /intents: that page talks to Chat only through twake-meet-embed', () => {
    window.history.pushState({}, '', `/intents`)
    render(<AppInitialization />)
    expect(bridge.created).not.toHaveBeenCalled()
  })
})
