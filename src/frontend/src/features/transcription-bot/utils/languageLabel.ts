// Human-readable language names for the LinTO translation UI.
//
// Translation targets travel as bare codes ("de", "es", "pt-BR"…); a 2-letter
// code is meaningless to most users. `Intl.DisplayNames` (standard, no dep)
// resolves a code to a full name LOCALIZED to the current UI language — e.g.
// in a French UI: de → "allemand", es → "espagnol". Mirrors the official Studio
// frontend (SessionTranslationSelection.vue).

const cache = new Map<string, Intl.DisplayNames>()

const displayNames = (locale: string): Intl.DisplayNames | null => {
  const key = locale || 'en'
  const hit = cache.get(key)
  if (hit) return hit
  try {
    const dn = new Intl.DisplayNames([key], { type: 'language' })
    cache.set(key, dn)
    return dn
  } catch {
    return null
  }
}

// Some providers send region/script subtags ("pt-BR", "zh-Hans") that
// DisplayNames resolves; others send odd casings. Normalize defensively.
const normalize = (code: string): string => code.trim().replace('_', '-')

/** Full localized language name, e.g. "Allemand". Falls back to the upper-cased
 *  code when the runtime can't resolve it. Capitalized for label use. */
export const languageName = (code: string, locale: string): string => {
  const norm = normalize(code)
  if (!norm) return ''
  const name = displayNames(locale)?.of(norm)
  if (!name || name === norm) return norm.toUpperCase()
  return name.charAt(0).toUpperCase() + name.slice(1)
}

/** "Allemand (de)" — name + the raw code, for the picker where the code still
 *  matters (admins map profiles by code). */
export const languageLabel = (code: string, locale: string): string => {
  const norm = normalize(code)
  const name = languageName(norm, locale)
  return name.toUpperCase() === norm.toUpperCase()
    ? norm.toUpperCase()
    : `${name} (${norm})`
}
