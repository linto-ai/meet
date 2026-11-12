// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  backgroundTokenOf,
  contrastOf,
  textTokenOf,
  type Mode,
} from '@/test/themeContrast.test-helper'
import { SidePanel } from './SidePanel'

/*
 * The close button of the side panel is an icon on the panel background: the
 * icon needs 3:1 (WCAG non-text contrast) in the light and the dark theme.
 */

vi.mock('../hooks/useSidePanel', () => ({
  PanelId: { CHAT: 'chat' },
  useSidePanel: () => ({
    activePanelId: 'chat',
    isSidePanelOpen: true,
    isChatOpen: true,
    isParticipantsOpen: false,
    isEffectsOpen: false,
    isToolsOpen: false,
    isAdminOpen: false,
    isInfoOpen: false,
    isSubPanelOpen: false,
    activeSubPanelId: null,
  }),
}))
vi.mock('@/features/reactions/hooks/useReactionsToolbar', () => ({
  useReactionsToolbar: () => ({ isOpen: false }),
}))
vi.mock('@/hooks/useRestoreFocus', () => ({ useRestoreFocus: () => {} }))
vi.mock('@/hooks/useEscapeToClose', () => ({ useEscapeToClose: () => {} }))
vi.mock('@/features/chat/components/Chat', () => ({ Chat: () => null }))
vi.mock('@/features/participants/components/ParticipantsList', () => ({
  ParticipantsList: () => null,
}))
vi.mock('./effects/Effects', () => ({ Effects: () => null }))
vi.mock('./Admin', () => ({ Admin: () => null }))
vi.mock('./Tools', () => ({ Tools: () => null }))
vi.mock('./Info', () => ({ Info: () => null }))

describe('SidePanel close button', () => {
  afterEach(cleanup)

  it.each<Mode>(['light', 'dark'])(
    'its icon is visible on the panel (%s)',
    (mode) => {
      render(<SidePanel />)
      const close = screen.getByRole('button', { name: /close/i })
      const ratio = contrastOf(
        textTokenOf(close),
        backgroundTokenOf(close),
        mode
      )
      expect(ratio).toBeGreaterThanOrEqual(3)
    }
  )
})
