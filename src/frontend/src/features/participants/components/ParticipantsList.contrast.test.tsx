// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  backgroundTokenOf,
  contrastOf,
  textTokenOf,
  type Mode,
} from '@/test/themeContrast.test-helper'
import { SidePanel } from '@/features/rooms/livekit/components/SidePanel'

/* The "IN ROOM" heading is 14 px text on the side panel (box.bg): 4.5:1 in both themes. */

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (k: string) => (k === 'subheading' ? 'In room' : k),
  }),
}))
vi.mock('@/features/rooms/livekit/hooks/useSidePanel', () => ({
  PanelId: { PARTICIPANTS: 'participants' },
  useSidePanel: () => ({
    activePanelId: 'participants',
    isSidePanelOpen: true,
    isParticipantsOpen: true,
    isChatOpen: false,
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
vi.mock('@/features/rooms/livekit/components/effects/Effects', () => ({
  Effects: () => null,
}))
vi.mock('@/features/rooms/livekit/components/Admin', () => ({
  Admin: () => null,
}))
vi.mock('@/features/rooms/livekit/components/Tools', () => ({
  Tools: () => null,
}))
vi.mock('@/features/rooms/livekit/components/Info', () => ({
  Info: () => null,
}))
vi.mock('./WaitingParticipantsSection', () => ({
  WaitingParticipantsSection: () => null,
}))
vi.mock('./ParticipantsCollapsibleSection', () => ({
  ParticipantsCollapsibleSection: () => null,
}))
vi.mock('@livekit/components-react', () => ({
  useRemoteParticipants: () => [],
  useRoomContext: () => ({
    localParticipant: { identity: 'me', attributes: {} },
  }),
}))

describe('Participants panel heading contrast', () => {
  afterEach(cleanup)

  it.each<Mode>(['light', 'dark'])('"In room" is readable (%s)', (mode) => {
    render(<SidePanel />)
    const h = screen.getByRole('heading', { name: /in room/i })
    const ratio = contrastOf(textTokenOf(h), backgroundTokenOf(h), mode)
    expect(ratio).toBeGreaterThanOrEqual(4.5)
  })
})
