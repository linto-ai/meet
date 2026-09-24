import { useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { lintoStore } from '../store/lintoStore'
import { transcriptStore } from '../store/transcriptStore'
import { baseCode, languageName, ORIGINAL } from '../utils/languageLabel'

/**
 * Languages the live captions can be DISPLAYED in, shared by the LinTO panel
 * selector and the CC language menu: the chosen translation targets UNIONed
 * with whatever translation has actually arrived, as base codes sorted by
 * localized name.
 *
 * `effective` is the stored display language when it is still offered, else
 * the original (a stale pick from a previous run never blanks the captions).
 */
export const useLintoDisplayLanguages = () => {
  const { i18n } = useTranslation()
  const { selectedTranslations, displayLanguage } = useSnapshot(lintoStore)
  const { byId, order } = useSnapshot(transcriptStore)

  const languages = useMemo(() => {
    const set = new Set<string>(selectedTranslations.map(baseCode))
    for (const id of order) {
      const entry = byId[id]
      if (!entry?.translations || !entry.text.trim()) continue
      for (const lang of Object.keys(entry.translations))
        set.add(baseCode(lang))
    }
    return Array.from(set).sort((a, b) =>
      languageName(a, i18n.language).localeCompare(
        languageName(b, i18n.language)
      )
    )
  }, [byId, order, selectedTranslations, i18n.language])

  const effective =
    displayLanguage === ORIGINAL || languages.includes(displayLanguage)
      ? displayLanguage
      : ORIGINAL

  const setDisplay = useCallback((value: string) => {
    lintoStore.displayLanguage = value
  }, [])

  return { languages, effective, setDisplay }
}
