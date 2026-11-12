// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  backgroundTokenOf,
  contrastOf,
  textTokenOf,
  type Mode,
} from '@/test/themeContrast.test-helper'
import { Chat } from './Chat'
import { ChatTextArea } from './ChatTextArea'

/*
 * Chat panel in the dark theme of /embed: the notice, and the placeholder of
 * the message field, must stay readable (WCAG AA, 4.5:1) in both themes.
 * Colours are the tokens carried by the classes, resolved as the browser
 * does (livekit.css redefines them under data-lk-theme='visio-dark').
 */

vi.mock('./ChatMessages', () => ({ ChatMessages: () => null }))
vi.mock('./ChatSubmitButton', () => ({ ChatSubmitButton: () => null }))

const MODES: Mode[] = ['light', 'dark']

describe('Chat panel contrast', () => {
  afterEach(cleanup)

  it.each(MODES)(
    'the notice about visible messages is readable (%s)',
    (mode) => {
      render(<Chat />)
      const notice = screen.getByText(/disclaimer/i)
      const ratio = contrastOf(
        textTokenOf(notice),
        backgroundTokenOf(notice),
        mode
      )
      expect(ratio).toBeGreaterThanOrEqual(4.5)
    }
  )

  it.each(MODES)('the message placeholder is readable (%s)', (mode) => {
    render(<ChatTextArea />)
    const field = screen.getByRole('textbox')
    const ratio = contrastOf(
      textTokenOf(field, 'placeholder:c_'),
      backgroundTokenOf(field),
      mode
    )
    expect(ratio).toBeGreaterThanOrEqual(4.5)
  })
})
