// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { connectionObserverStore } from '@/stores/connectionObserver'
import { navigateTo } from '@/navigation/navigateTo'
import { EmbedIdleContext } from '@/features/embed/EmbedIdleContext'
import { IsIdleDisconnectModal } from './IsIdleDisconnectModal'

/*
 * The idle dialog: "Leave" and the end of the countdown leave the call. On
 * /feedback outside the embed; framed by Chat, they tell the page (which says
 * left{idle} to Chat) and never navigate.
 */

vi.mock('@/navigation/navigateTo', () => ({ navigateTo: vi.fn() }))
vi.mock('@/hooks/useScreenReaderAnnounce', () => ({
  useScreenReaderAnnounce: () => () => {},
}))
vi.mock('@/hooks/useHumanizeDuration', () => ({
  useHumanizeDuration: () => (ms: number) => `${ms}ms`,
}))

const COUNTDOWN_MS = 120_000

const renderModal = (onIdle?: () => void) =>
  render(
    <EmbedIdleContext.Provider value={onIdle ?? null}>
      <IsIdleDisconnectModal />
    </EmbedIdleContext.Provider>
  )

const leaveButton = () => screen.getByRole('button', { name: /leaveButton/ })

describe('idle disconnect dialog', () => {
  beforeEach(() => {
    connectionObserverStore.isIdleDisconnectModalOpen = true
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.mocked(navigateTo).mockClear()
    connectionObserverStore.isIdleDisconnectModalOpen = false
  })

  describe('framed by Chat (/embed)', () => {
    it('Leave tells the page it is idle, closes the dialog, no /feedback', () => {
      const onIdle = vi.fn()
      renderModal(onIdle)
      fireEvent.click(leaveButton())
      expect(onIdle).toHaveBeenCalledTimes(1)
      expect(connectionObserverStore.isIdleDisconnectModalOpen).toBe(false)
      expect(navigateTo).not.toHaveBeenCalled()
    })

    it('the countdown reaching 0 tells the page, once, no /feedback', () => {
      vi.useFakeTimers()
      const onIdle = vi.fn()
      renderModal(onIdle)
      act(() => {
        vi.advanceTimersByTime(COUNTDOWN_MS - 1000)
      })
      expect(onIdle).not.toHaveBeenCalled()
      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(onIdle).toHaveBeenCalledTimes(1)
      act(() => {
        vi.advanceTimersByTime(COUNTDOWN_MS)
      })
      expect(onIdle).toHaveBeenCalledTimes(1)
      expect(navigateTo).not.toHaveBeenCalled()
    })
  })

  describe('as a normal Meet page', () => {
    it('Leave goes to /feedback', () => {
      renderModal()
      fireEvent.click(leaveButton())
      expect(navigateTo).toHaveBeenCalledWith('feedback', {
        duplicateIdentity: false,
      })
      expect(connectionObserverStore.isIdleDisconnectModalOpen).toBe(false)
    })

    it('the countdown reaching 0 goes to /feedback', () => {
      vi.useFakeTimers()
      renderModal()
      act(() => {
        vi.advanceTimersByTime(COUNTDOWN_MS)
      })
      expect(navigateTo).toHaveBeenCalledWith('feedback', {
        duplicateIdentity: false,
      })
    })
  })
})
