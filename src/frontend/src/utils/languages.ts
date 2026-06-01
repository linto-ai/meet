// Map frontend language codes to backend language codes

export type BackendLanguage =
  | 'en-us'
  | 'fr-fr'
  | 'nl-nl'
  | 'de-de'
  | 'ru-ru'
  | 'vi-vn'
export type FrontendLanguage = 'en' | 'fr' | 'nl' | 'de' | 'ru' | 'vi'

const frontendToBackendMap: Record<FrontendLanguage, BackendLanguage> = {
  en: 'en-us',
  fr: 'fr-fr',
  nl: 'nl-nl',
  de: 'de-de',
  ru: 'ru-ru',
  vi: 'vi-vn',
}

export const convertToBackendLanguage = (
  frontendLang: string = 'fr'
): BackendLanguage => {
  return frontendToBackendMap[frontendLang as FrontendLanguage]
}
