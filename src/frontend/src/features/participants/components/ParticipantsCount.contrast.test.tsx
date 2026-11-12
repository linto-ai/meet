// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  backgroundTokenOf,
  contrastOf,
  textTokenOf,
  type Mode,
} from '@/test/themeContrast.test-helper'
import { ParticipantsCount } from './ParticipantsCount'

/* The count badge is 12 px text: 4.5:1 against its own fill in both themes. */

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}))
vi.mock('@livekit/components-react', () => ({
  useRemoteParticipants: () => [],
}))

describe('ParticipantsCount badge contrast', () => {
  afterEach(cleanup)

  it.each<Mode>(['light', 'dark'])('its number is readable (%s)', (mode) => {
    const { getByText } = render(<ParticipantsCount describedById="x" />)
    const badge = getByText('1')
    const ratio = contrastOf(textTokenOf(badge), backgroundTokenOf(badge), mode)
    expect(ratio).toBeGreaterThanOrEqual(4.5)
  })
})
