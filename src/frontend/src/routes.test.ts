import { describe, expect, it } from 'vitest'
import { routes } from './routes'

const match = (path: RegExp | string, url: string) =>
  path instanceof RegExp
    ? (path.exec(url)?.groups ?? null)
    : path === url
      ? {}
      : null

describe('intent service routes', () => {
  it('opens /intents, whatever its query', () => {
    expect(routes.intentService.name).toBe('intentService')
    expect(match(routes.intentService.path, '/intents')).not.toBeNull()
  })

  it.each(['/intents/', '/intents/x', '/intent'])(
    'does not open %s as the service page',
    (url) => {
      expect(match(routes.intentService.path, url)).toBeNull()
    }
  )

  it('opens /intents/signed-in on its own route', () => {
    expect(
      match(routes.intentSignedIn.path, '/intents/signed-in')
    ).not.toBeNull()
    expect(match(routes.intentService.path, '/intents/signed-in')).toBeNull()
  })
})
