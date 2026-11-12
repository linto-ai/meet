// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { css } from '@/styled-system/css'
import { ResponsiveMenu } from './ResponsiveMenu'

/*
 * The compact menu is a bottom sheet. In a small iframe (242 px high) it is
 * taller than the frame: it must be bounded by the viewport and scroll, so
 * that every item stays reachable. Only the styling can be seen in jsdom;
 * the real heights are for the browser pass.
 */

const scrollable = css({ overflowY: 'auto' }).split(' ')

const sheetAncestors = () => {
  const out: HTMLElement[] = []
  for (
    let n: HTMLElement | null = screen.getByRole('dialog');
    n && n !== document.body;
    n = n.parentElement
  ) {
    out.push(n)
  }
  return out
}

describe('ResponsiveMenu', () => {
  afterEach(cleanup)

  const open = () =>
    render(
      <ResponsiveMenu isOpened onClosed={() => {}}>
        <button>Share screen</button>
      </ResponsiveMenu>
    )

  it('scrolls vertically when its items do not fit the frame', () => {
    open()
    const scrolls = sheetAncestors().some((el) =>
      scrollable.every((c) => el.classList.contains(c))
    )
    expect(scrolls).toBe(true)
  })

  it('is bounded in height by the viewport', () => {
    open()
    const bounded = sheetAncestors().some((el) =>
      [...el.classList].some((c) => c.startsWith('max-h_'))
    )
    expect(bounded).toBe(true)
  })

  it('keeps its items in the dialog', () => {
    open()
    expect(screen.getByRole('button', { name: 'Share screen' })).toBeTruthy()
  })
})
