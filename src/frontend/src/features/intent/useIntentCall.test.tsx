// @vitest-environment jsdom
import { type ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { IntentBridgeContext } from './IntentBridgeContext'
import type { HostBridge } from './intentEvents'
import { useCallEvents, useFinish } from './useIntentCall'

/*
 * The hooks of the call on /intents read the bridge to the host from
 * IntentBridgeContext: no bridge (nor a ref to it) goes through their
 * arguments (review of linto-ai/meet#18).
 */

const fakeBridge = (): HostBridge => ({
  joined: vi.fn(),
  left: vi.fn(),
  error: vi.fn(),
  close: vi.fn(),
})

const withBridge = (bridge: HostBridge | null) =>
  function BridgeWrapper({ children }: { children: ReactNode }) {
    return (
      <IntentBridgeContext.Provider value={bridge}>
        {children}
      </IntentBridgeContext.Provider>
    )
  }

describe('useFinish', () => {
  it('tells the host of the context that the call is over, once', () => {
    const bridge = fakeBridge()
    const { result } = renderHook(() => useFinish(), {
      wrapper: withBridge(bridge),
    })
    act(() => result.current.finish('user'))
    act(() => result.current.finish('idle'))
    expect(bridge.left).toHaveBeenCalledTimes(1)
    expect(bridge.left).toHaveBeenCalledWith('user')
    expect(result.current.isOver).toBe(true)
  })

  it('says left (idle) on onIdle', () => {
    const bridge = fakeBridge()
    const { result } = renderHook(() => useFinish(), {
      wrapper: withBridge(bridge),
    })
    act(() => result.current.onIdle())
    expect(bridge.left).toHaveBeenCalledWith('idle')
  })

  it('ends the call without a host to tell', () => {
    const { result } = renderHook(() => useFinish(), {
      wrapper: withBridge(null),
    })
    act(() => result.current.finish('user'))
    expect(result.current.isOver).toBe(true)
  })
})

describe('useCallEvents', () => {
  const render = (bridge: HostBridge | null) => {
    const finish = vi.fn()
    const isLeavingRef = { current: false }
    const { result } = renderHook(
      () => useCallEvents(false, isLeavingRef, finish),
      { wrapper: withBridge(bridge) }
    )
    return { events: result.current, finish }
  }

  it('says joined to the host of the context once LiveKit is connected', () => {
    const bridge = fakeBridge()
    const { events } = render(bridge)
    act(() => events.onConnected())
    expect(bridge.joined).toHaveBeenCalledTimes(1)
  })

  it('says error to the host of the context when the join fails', () => {
    const bridge = fakeBridge()
    const { events } = render(bridge)
    act(() => events.onError(new Error('boom')))
    expect(bridge.error).toHaveBeenCalledWith('unknown')
  })

  it('finishes with the LiveKit reason once joined', () => {
    const { events, finish } = render(fakeBridge())
    act(() => events.onConnected())
    act(() => events.onDisconnected())
    expect(finish).toHaveBeenCalledWith('disconnected')
  })
})
