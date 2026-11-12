// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { Button } from 'react-aria-components'
import { afterEach, describe, expect, it } from 'vitest'
import {
  backgroundTokenOf,
  contrastOf,
  textTokenOf,
  type Mode,
} from '@/test/themeContrast.test-helper'
import { TooltipWrapper } from './TooltipWrapper'

/* The control bar tooltips (Leave, Raise hand, More options): 4.5:1 in both themes. */
describe('TooltipWrapper contrast', () => {
  afterEach(cleanup)

  it.each<Mode>(['light', 'dark'])(
    'its text is readable on its background (%s)',
    async (mode) => {
      render(
        <TooltipWrapper tooltip="Leave the meeting" tooltipType="instant">
          <Button>Leave</Button>
        </TooltipWrapper>
      )
      const button = screen.getByRole('button')
      // keyboard modality, then focus: react-aria opens the tooltip
      fireEvent.keyDown(document.body, { key: 'Tab' })
      act(() => button.focus())
      const tip = await screen.findByRole('tooltip')
      const ratio = contrastOf(textTokenOf(tip), backgroundTokenOf(tip), mode)
      expect(ratio).toBeGreaterThanOrEqual(4.5)
    }
  )
})
