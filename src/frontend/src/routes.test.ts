import { describe, expect, it } from 'vitest'
import { routes } from './routes'

const UUID = '3f2b8c1e-9d4a-4e0b-8a57-6c1d2e9f0a11'

const match = (path: RegExp | string, url: string) =>
  path instanceof RegExp
    ? (path.exec(url)?.groups ?? null)
    : path === url
      ? {}
      : null

describe('embed route', () => {
  it('opens /embed/<uuid> with the uuid as roomId', () => {
    expect(match(routes.embedRoom.path, `/embed/${UUID}`)).toEqual({
      roomId: UUID,
    })
  })

  it.each([
    '/embed',
    '/embed/',
    '/embed/not-a-uuid',
    `/embed/${UUID}/extra`,
    `/embed/${UUID}x`,
    `/${UUID}`,
  ])('does not open %s', (url) => {
    expect(match(routes.embedRoom.path, url)).toBeNull()
  })

  it('is built from a uuid', () => {
    expect(routes.embedRoom.to?.(UUID)).toBe(`/embed/${UUID}`)
  })

  it('does not turn an ordinary room route into an embed', () => {
    expect(match(routes.embedRoom.path, '/abc-defg-hij')).toBeNull()
    expect(match(routes.room.path, `/embed/${UUID}`)).toBeNull()
  })
})
