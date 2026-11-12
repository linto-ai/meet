import { token } from '@/styled-system/tokens'
// @ts-expect-error @types/node is not installed; vitest runs in node
import { readFileSync } from 'node:fs'

/*
 * Resolves a Panda colour token (as it appears in an atomic class such as
 * `bg-c_box.bg` or `c_primary.900`) to a hex value in the light or in the dark
 * theme of /embed, then computes a WCAG contrast ratio. The dark theme is
 * the `[data-lk-theme='visio-dark']` block of livekit.css: it redefines CSS
 * variables of tokens, and every token that references them follows, as the
 * browser does with var(). Values come from the generated Panda tokens.
 */

// Vitest serves an empty string for css, so the file is read from disk (tests run from the frontend root)
const livekitCss: string = readFileSync('src/styles/livekit.css', 'utf8')

const tokenValue = (path: string): string | undefined =>
  (token as (p: string) => string | undefined)(path)

// `--colors-primary-dark-50` -> value of the token `colors.primaryDark.50`
const valueOfVar = (name: string): string => {
  const parts = name.replace(/^--colors-/, '').split('-')
  for (let mask = 0; mask < 1 << (parts.length - 1); mask++) {
    const groups: string[][] = [[parts[0]]]
    parts.slice(1).forEach((part, i) => {
      if (mask & (1 << i)) groups.push([part])
      else groups[groups.length - 1].push(part)
    })
    const path = groups
      .map((g) =>
        g.map((w, i) => (i ? w[0].toUpperCase() + w.slice(1) : w)).join('')
      )
      .join('.')
    const value = tokenValue(`colors.${path}`)
    if (value !== undefined) return value
  }
  throw new Error(`unknown colour variable ${name}`)
}

const darkOverrides = (() => {
  const out = new Map<string, string>()
  for (const [, body] of livekitCss.matchAll(
    /\[data-lk-theme='visio-dark'\]\s*\{([^}]*)\}/g
  )) {
    for (const [, from, to] of body.matchAll(
      /(--colors-[\w-]+):\s*var\((--colors-[\w-]+)\)/g
    )) {
      out.set(from, to)
    }
  }
  return out
})()

const resolveVar = (name: string, mode: Mode): string => {
  const override = mode === 'dark' ? darkOverrides.get(name) : undefined
  if (override) return resolveVar(override, mode)
  return resolveValue(valueOfVar(name), mode)
}

const resolveValue = (value: string, mode: Mode): string => {
  const ref = /^var\((--colors-[\w-]+)\)$/.exec(value)
  if (ref) return resolveVar(ref[1], mode)
  if (/^#[0-9a-f]{3}$/i.test(value)) {
    return '#' + [...value.slice(1)].map((c) => c + c).join('')
  }
  if (/^#[0-9a-f]{6}$/i.test(value)) return value
  throw new Error(`cannot read colour ${value}`)
}

export type Mode = 'light' | 'dark'

export const resolveColor = (name: string, mode: Mode): string => {
  const raw = tokenValue(`colors.${name}`)
  if (raw === undefined) throw new Error(`unknown colour token "${name}"`)
  // the variable of the token itself is the one the theme may redefine
  const varName = `--colors-${name
    .replace(/([a-z])([A-Z])/g, '$1-$2')
    .replace(/\./g, '-')
    .toLowerCase()}`
  return resolveVar(varName, mode)
}

const luminance = (hex: string) => {
  const [r, g, b] = [1, 3, 5]
    .map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

export const contrast = (a: string, b: string) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const tokenOf = (el: Element, prefixes: string[]): string | null => {
  for (const cls of el.classList) {
    for (const p of prefixes) {
      if (cls.startsWith(p) && !cls.includes('[')) return cls.slice(p.length)
    }
  }
  return null
}

/** Token of the background in effect: the nearest ancestor-or-self with one. */
export const backgroundTokenOf = (el: Element): string => {
  for (let n: Element | null = el; n; n = n.parentElement) {
    const t = tokenOf(n, ['bg-c_', 'bg_'])
    if (t && t !== 'none!') return t
  }
  return 'default.bg'
}

/** Token of the text colour in effect (inherited; the page text by default). */
export const textTokenOf = (el: Element, prefix = 'c_'): string => {
  for (let n: Element | null = el; n; n = n.parentElement) {
    const t = tokenOf(n, [prefix])
    if (t) return t
  }
  return 'default.text'
}

export const contrastOf = (fg: string, bg: string, mode: Mode) =>
  contrast(resolveColor(fg, mode), resolveColor(bg, mode))
