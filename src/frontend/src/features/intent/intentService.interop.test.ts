// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/*
 * The production build (vite 8) compiles `import Intents from 'cozy-interapp'`
 * with Node interop: the default import is the CJS exports object
 * ({ default: Intents, ... }), not the class. Other specs mock the class
 * straight in `default`, so they cannot see it. Here the module is mocked in
 * both shapes it can reach the page in.
 */

const ROOM = '3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11'

vi.mock('@/api/fetchApi', () => ({
  fetchApi: vi
    .fn()
    .mockResolvedValue({ access_token: 's', platform_url: 'https://p.test' }),
}))

const createService = vi.fn()
class FakeIntents {
  createService = createService
}

const shapes = [
  ['the class directly in default', FakeIntents],
  [
    'the CJS exports object in default (build interop)',
    { default: FakeIntents },
  ],
] as const

describe('startIntentService with cozy-interapp as it is shipped', () => {
  beforeEach(() => {
    vi.resetModules()
    createService.mockReset()
    createService.mockResolvedValue({
      getData: vi.fn().mockResolvedValue({
        room: ROOM,
        token: 'livekit',
        audio: true,
        video: false,
      }),
      notifyReadyToUse: vi.fn(),
      sendResult: vi.fn(),
      terminate: vi.fn(),
      onData: vi.fn().mockReturnValue(() => {}),
    })
    window.history.pushState({}, '', '/intents?intent=intent-42')
  })

  afterEach(() => {
    vi.doUnmock('cozy-interapp')
    window.history.pushState({}, '', '/')
  })

  it.each(shapes)('reaches the service with %s', async (_name, exported) => {
    vi.doMock('cozy-interapp', () => ({ default: exported }))
    const { startIntentService } = await import('./intentService')
    const onJoin = vi.fn()
    const onError = vi.fn()
    startIntentService({
      onJoin,
      onLeave: vi.fn(),
      onError,
    })
    await vi.waitFor(() =>
      expect(createService).toHaveBeenCalledWith('intent-42', window)
    )
    expect(onError).not.toHaveBeenCalledWith('unsupported')
    await vi.waitFor(() => expect(onJoin).toHaveBeenCalled())
  })
})
