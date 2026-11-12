// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import {
  backgroundTokenOf,
  contrastOf,
  textTokenOf,
  type Mode,
} from '@/test/themeContrast.test-helper'
import { VisualOnlyTooltip } from './VisualOnlyTooltip'

/* The tooltip text must stay readable (4.5:1) on its own background in both themes. */
describe('VisualOnlyTooltip contrast', () => {
  afterEach(cleanup)

  it.each<Mode>(['light', 'dark'])(
    'its text is readable on its background (%s)',
    (mode) => {
      render(
        <VisualOnlyTooltip tooltip="Mute microphone">
          <button>Mic</button>
        </VisualOnlyTooltip>
      )
      act(() => {
        fireEvent.mouseEnter(screen.getByRole('button').parentElement!)
      })
      const tip = screen.getByText('Mute microphone')
      const ratio = contrastOf(textTokenOf(tip), backgroundTokenOf(tip), mode)
      expect(ratio).toBeGreaterThanOrEqual(4.5)
    }
  )
})
