// @vitest-environment jsdom
import { type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchUser } from './fetchUser'
import { useUser } from './useUser'

vi.mock('./fetchUser', () => ({
  fetchUser: vi.fn(() => Promise.resolve(false)),
}))
vi.mock('@/api/useConfig', () => ({
  useConfig: () => ({
    data: { is_silent_login_enabled: true },
    isLoading: false,
  }),
}))

const lookUpUser = async () => {
  const client = new QueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  renderHook(() => useUser(), { wrapper })
  await waitFor(() => expect(fetchUser).toHaveBeenCalled())
  return vi.mocked(fetchUser).mock.calls[0][0]
}

describe('silent login', () => {
  afterEach(() => {
    vi.mocked(fetchUser).mockClear()
    window.history.pushState({}, '', '/')
  })

  it('is off under /intents: an IdP redirect would break the iframe', async () => {
    window.history.pushState({}, '', `/intents`)
    expect(await lookUpUser()).toMatchObject({ attemptSilent: false })
  })

  it('stays on elsewhere when the instance enables it', async () => {
    window.history.pushState({}, '', '/abc-defg-hij')
    expect(await lookUpUser()).not.toMatchObject({ attemptSilent: false })
  })
})
