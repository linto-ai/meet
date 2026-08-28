import { css } from '@/styled-system/css'
import { Text } from '@/primitives'
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { transcriptStore } from '../store/transcriptStore'
import { lintoStore } from '../store/lintoStore'
import { LintoCaption } from '../types/linto'
import { languageName } from '../utils/languageLabel'

interface LintoTurn {
  locutor: string
  text: string
  translations?: Record<string, string>
  partial: boolean
}

const ORIGINAL = 'original'

// Targets are requested as short codes ("en", "de") but a provider may echo a
// region-tagged variant ("en-US"). Collapse to the base code so the selector
// has no duplicates and a short-code pick still matches a tagged translation.
const baseCode = (code: string): string => code.split('-')[0].toLowerCase()

// Find the translation for a (base) language inside a turn, tolerant of region
// tags on the stored keys.
const translationFor = (
  translations: Record<string, string> | undefined,
  lang: string
): string | undefined => {
  if (!translations) return undefined
  if (translations[lang] != null) return translations[lang]
  const hit = Object.entries(translations).find(([k]) => baseCode(k) === lang)
  return hit?.[1]
}

const selectClass = css({
  width: '100%',
  padding: '0.4rem',
  borderRadius: '4px',
  border: '1px solid',
  borderColor: 'control.border',
  backgroundColor: 'white',
})

/**
 * Group consecutive captions by speaker into turns (same idea as
 * `Subtitles.tsx`'s row grouping), then render one line per turn.
 *
 * A partial (in-flight) caption always starts its own turn so it can be rendered
 * dimmed; only consecutive FINAL captions of the same speaker are merged.
 */
const groupByLocutor = (captions: LintoCaption[]): LintoTurn[] => {
  const turns: LintoTurn[] = []
  for (const caption of captions) {
    if (!caption.text.trim()) continue
    const last = turns[turns.length - 1]
    if (
      last &&
      last.locutor === caption.locutor &&
      !last.partial &&
      !caption.partial
    ) {
      last.text = `${last.text} ${caption.text}`.trim()
      if (caption.translations) {
        last.translations = { ...last.translations, ...caption.translations }
      }
    } else {
      turns.push({
        locutor: caption.locutor,
        text: caption.text,
        translations: caption.translations,
        partial: caption.partial,
      })
    }
  }
  return turns
}

export const LiveTranscript = () => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'lintoBot',
  })
  const { selectedTranslations, displayLanguage } = useSnapshot(lintoStore)
  const { byId, order } = useSnapshot(transcriptStore)
  const turns = useMemo(
    () =>
      groupByLocutor(
        order.map((id) => byId[id]).filter((c): c is LintoCaption => !!c)
      ),
    [byId, order]
  )

  // Languages offered by the "displayed language" selector: the chosen targets
  // (so the starter sees them immediately) UNIONed with whatever has actually
  // arrived in the captions (so any viewer — who never chose anything — still
  // gets every live language). Sorted by their localized name.
  const availableLangs = useMemo(() => {
    const set = new Set<string>(selectedTranslations.map(baseCode))
    for (const turn of turns) {
      if (turn.translations) {
        for (const lang of Object.keys(turn.translations))
          set.add(baseCode(lang))
      }
    }
    return Array.from(set).sort((a, b) =>
      languageName(a, i18n.language).localeCompare(
        languageName(b, i18n.language)
      )
    )
  }, [turns, selectedTranslations, i18n.language])

  // The displayed language is SHARED with the caption overlay (lintoStore), so
  // changing it here also switches the subtitles.
  const setDisplay = (value: string) => {
    lintoStore.displayLanguage = value
  }
  // If the selected language is no longer offered (run changed), fall back.
  const effectiveDisplay =
    displayLanguage === ORIGINAL || availableLangs.includes(displayLanguage)
      ? displayLanguage
      : ORIGINAL

  return (
    <div
      data-testid="linto-live-transcript"
      className={css({
        width: '100%',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.75rem',
        marginBottom: '1rem',
      })}
    >
      <Text variant="h3" margin={false}>
        {t('live.heading')}
      </Text>

      {availableLangs.length > 0 && (
        <label className={css({ width: '100%' })}>
          <Text variant="sm">{t('live.displayLanguage')}</Text>
          <select
            data-testid="linto-display-language"
            className={selectClass}
            value={effectiveDisplay}
            onChange={(e) => setDisplay(e.target.value)}
          >
            <option value={ORIGINAL}>{t('live.original')}</option>
            {availableLangs.map((lang) => (
              <option key={lang} value={lang}>
                {languageName(lang, i18n.language)}
              </option>
            ))}
          </select>
        </label>
      )}

      {turns.length === 0 ? (
        <Text variant="smNote">{t('live.empty')}</Text>
      ) : (
        turns.map((turn, index) => {
          const showTranslated = effectiveDisplay !== ORIGINAL
          const translated = translationFor(turn.translations, effectiveDisplay)
          // When the chosen language hasn't arrived for THIS turn yet, fall back
          // to the original text so the line never blanks out mid-stream.
          const body = showTranslated ? (translated ?? turn.text) : turn.text
          const isTranslated = showTranslated && translated != null
          return (
            <div
              key={`${turn.locutor}-${index}`}
              data-testid="linto-turn"
              data-speaker={turn.locutor}
              data-partial={turn.partial ? 'true' : 'false'}
              className={css({
                width: '100%',
                opacity: turn.partial ? 0.6 : 1,
                fontStyle: turn.partial ? 'italic' : 'normal',
              })}
            >
              <Text
                variant="sm"
                className={css({
                  fontWeight: 'semibold',
                  color: 'primary.700',
                })}
              >
                {turn.locutor}
              </Text>
              {isTranslated ? (
                <div
                  data-testid="linto-translation"
                  data-lang={effectiveDisplay}
                >
                  <Text variant="sm">{body}</Text>
                </div>
              ) : (
                <Text variant="sm">{body}</Text>
              )}
            </div>
          )
        })
      )}
    </div>
  )
}
