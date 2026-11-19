import { describe, expect, it } from 'vitest'
import en from './en/global.json'
import fr from './fr/global.json'

/* Strings of the intent service page (/intents), shown inside the iframe. */

const CODES = ['unauthenticated', 'popup_blocked', 'unreachable', 'unsupported']

describe.each([
  ['en', en, 'Sign in to Meet'],
  ['fr', fr, 'Se connecter à Meet'],
] as const)('intent page strings in %s', (_lng, json, signIn) => {
  const intent = (json as { embed?: { intent?: Record<string, unknown> } })
    .embed?.intent

  it('labels the sign-in button', () => {
    expect(intent?.signIn).toBe(signIn)
  })

  it.each(CODES)('has a short message for the error %s', (code) => {
    const message = (intent?.error as Record<string, string> | undefined)?.[
      code
    ]
    expect(typeof message).toBe('string')
    expect(message!.trim().length).toBeGreaterThan(0)
    expect(message!.length).toBeLessThan(120)
  })
})
