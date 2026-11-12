// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { OptionsMenuItems } from './OptionsMenuItems'

/*
 * The options menu of the call. Framed by Chat (/embed/<uuid>) there is no Meet
 * session: Picture-in-picture, the transcript and the screen recording
 * (protocol twake-meet-embed v1: "no picture-in-picture, no recording/
 * transcript/admin actions that need a Meet session") are not offered, even
 * where the browser and the room would allow them. Elsewhere the menu is as
 * before. The neighbours are stubs; the three entries below are made available
 * by their own hooks, as on a desktop Chromium with a recording-capable room.
 */

vi.mock('@/features/pip/hooks/usePictureInPicture', () => ({
  usePictureInPicture: () => ({
    toggle: vi.fn(),
    isOpen: false,
    isSupported: true,
  }),
}))
vi.mock('@/features/recording', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useHasRecordingAccess: () => true,
}))
vi.mock('@/features/rooms/livekit/hooks/useSidePanel', () => ({
  useSidePanel: () => ({
    isTranscriptOpen: false,
    isScreenRecordingOpen: false,
    openTranscript: vi.fn(),
    openScreenRecording: vi.fn(),
    toggleTools: vi.fn(),
  }),
}))

const stub = vi.hoisted(() => ({
  item: (label: string) => async () => {
    const { MenuItem } = await import('react-aria-components')
    return { Item: () => <MenuItem>{label}</MenuItem> }
  },
}))
vi.mock('./FullScreenMenuItem', async () => ({
  FullScreenMenuItem: (await stub.item('Full screen')()).Item,
}))
vi.mock('./EffectsMenuItem', async () => ({
  EffectsMenuItem: (await stub.item('Effects')()).Item,
}))
vi.mock('./SupportMenuItem', async () => ({
  SupportMenuItem: (await stub.item('Support')()).Item,
}))
vi.mock('./DocumentationMenuItem', async () => ({
  DocumentationMenuItem: (await stub.item('Documentation')()).Item,
}))
vi.mock('./FeedbackMenuItem', async () => ({
  FeedbackMenuItem: (await stub.item('Feedback')()).Item,
}))
vi.mock('./SettingsMenuItem', async () => ({
  SettingsMenuItem: (await stub.item('Settings')()).Item,
}))

const renderAt = (path: string) => {
  window.history.pushState({}, '', path)
  return render(<OptionsMenuItems />)
}
const entry = (name: RegExp) => screen.queryByRole('menuitem', { name })

describe('options menu', () => {
  afterEach(() => {
    cleanup()
    window.history.pushState({}, '', '/')
  })

  describe('framed by Chat (/embed)', () => {
    it('does not offer picture-in-picture', () => {
      renderAt('/embed/3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11')
      expect(entry(/picture/i)).toBeNull()
    })

    it('does not offer the transcript', () => {
      renderAt('/embed/3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11')
      expect(entry(/transcript/i)).toBeNull()
    })

    it('does not offer the screen recording', () => {
      renderAt('/embed/3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11')
      expect(entry(/screenRecording/i)).toBeNull()
    })

    it('keeps the entries that need no session', () => {
      renderAt('/embed/3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11')
      expect(entry(/full screen/i)).not.toBeNull()
      expect(entry(/effects/i)).not.toBeNull()
    })
  })

  describe('outside the embed', () => {
    it('still offers picture-in-picture, the transcript and the screen recording', () => {
      renderAt('/some-room')
      expect(entry(/picture/i)).not.toBeNull()
      expect(entry(/transcript/i)).not.toBeNull()
      expect(entry(/screenRecording/i)).not.toBeNull()
    })
  })
})
