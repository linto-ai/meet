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

  it('has no sign-in route: the session exists before the host frames /intents', () => {
    expect(routes).not.toHaveProperty('intentSignedIn')
    const paths = Object.values(routes).map(({ path }) => String(path))
    expect(paths.filter((p) => p.includes('signed-in'))).toEqual([])
  })
})
