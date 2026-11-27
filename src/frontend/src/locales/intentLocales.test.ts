import { describe, expect, it } from 'vitest'
import de from './de/global.json'
import en from './en/global.json'
import es from './es/global.json'
import fr from './fr/global.json'
import nl from './nl/global.json'
import ru from './ru/global.json'
import vi from './vi/global.json'

/*
 * Strings of the intent service page (/intents), shown inside the iframe,
 * under `intent` (an intent, not an embed). The page never signs in: no
 * sign-in button, no popup to be blocked.
 */

const CODES = ['unauthenticated', 'unreachable', 'unsupported']

describe.each([
  ['de', de],
  ['en', en],
  ['es', es],
  ['fr', fr],
  ['nl', nl],
  ['ru', ru],
  ['vi', vi],
] as const)('intent page strings in %s', (_lng, json) => {
  const all = json as Record<string, unknown>
  const intent = all.intent as Record<string, unknown> | undefined
  const errors = intent?.error as Record<string, string> | undefined

  it.each(CODES)('has a short message for the error %s', (code) => {
    const message = errors?.[code]
    expect(typeof message).toBe('string')
    expect(message!.trim().length).toBeGreaterThan(0)
    expect(message!.length).toBeLessThan(120)
  })

  it('has no sign-in label and no popup_blocked message', () => {
    expect(intent).toBeDefined()
    expect(intent).not.toHaveProperty('signIn')
    expect(errors).not.toHaveProperty('popup_blocked')
  })

  it('keeps no strings under embed.intent', () => {
    expect(all.embed ?? {}).not.toHaveProperty('intent')
  })
})
