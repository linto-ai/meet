// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { css } from '@/styled-system/css'
import { Layout } from './Layout'

/*
 * In /embed the host can ask for the dark theme: the page wrapper must paint
 * with a semantic colour token (redefined by the dark theme), never with the
 * static white, which stays white whatever the theme.
 */

vi.mock('./Header', () => ({ Header: () => null }))
vi.mock('@/layout/Footer', () => ({ Footer: () => null }))
vi.mock('@/features/support/components/SupportWidget', () => ({
  SupportWidget: () => null,
}))

describe('Layout', () => {
  afterEach(cleanup)

  const wrapper = () => {
    render(
      <Layout>
        <p>content</p>
      </Layout>
    )
    return screen.getByRole('main').parentElement as HTMLElement
  }

  it('does not paint the page with the static white', () => {
    expect(wrapper().classList).not.toContain(css({ backgroundColor: 'white' }))
  })

  it('paints the page with the theme background', () => {
    expect(wrapper().classList).toContain(
      css({ backgroundColor: 'default.bg' })
    )
  })
})
