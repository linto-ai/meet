import { css } from '@/styled-system/css'
import { Button, Text } from '@/primitives'
import { Fragment, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { transcriptStore } from '../store/transcriptStore'
import { lintoStore } from '../store/lintoStore'
import { refreshLintoCatchUp } from '../hooks/useLintoCatchUp'
import { LintoCaption } from '../types/linto'
import { languageName } from '../utils/languageLabel'
import { SimpleMarkdown } from './SimpleMarkdown'

const ORIGINAL = 'original'

// Targets are requested as short codes ("en", "de") but a provider may echo a
// region-tagged variant ("en-US"). Collapse to the base code so the selector
// has no duplicates and a short-code pick still matches a tagged translation.
const baseCode = (code: string): string => code.split('-')[0].toLowerCase()

// Find the translation for a (base) language inside an entry, tolerant of region
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

// Wall-clock HH:MM of when the utterance was received (≈ when it was said).
const formatTime = (ms: number, locale: string): string => {
  if (!ms) return ''
  try {
    return new Date(ms).toLocaleTimeString(locale, {
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
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
 * Live transcript as a meeting LOG: one entry per caption (each final is its own
 * timestamped line, never concatenated with the previous one). The speaker name
 * heads a run of consecutive same-speaker entries; the in-flight partial shows
 * dimmed at the tail and refreshes in place.
 */
export const LiveTranscript = () => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'lintoBot',
  })
  const {
    selectedTranslations,
    displayLanguage,
    startedByMe,
    joinedAt,
    catchUp,
  } = useSnapshot(lintoStore)
  const { byId, order } = useSnapshot(transcriptStore)

  // One entry per caption, in arrival order (partials refresh their own id).
  const entries = useMemo(
    () =>
      order
        .map((id) => byId[id])
        .filter((c): c is LintoCaption => !!c && !!c.text.trim()),
    [byId, order]
  )

  // Languages offered by the "displayed language" selector: the chosen targets
  // UNIONed with whatever has actually arrived, sorted by localized name.
  const availableLangs = useMemo(() => {
    const set = new Set<string>(selectedTranslations.map(baseCode))
    for (const entry of entries) {
      if (entry.translations) {
        for (const lang of Object.keys(entry.translations))
          set.add(baseCode(lang))
      }
    }
    return Array.from(set).sort((a, b) =>
      languageName(a, i18n.language).localeCompare(
        languageName(b, i18n.language)
      )
    )
  }, [entries, selectedTranslations, i18n.language])

  // Catch-up: everything said BEFORE I joined is history, shown dimmed above a
  // "you joined at HH:MM" divider. The starter saw it all and gets neither.
  // `unavailable` (no LLM in this deployment) hides the summary block entirely.
  const showCatchUp =
    !startedByMe &&
    joinedAt !== null &&
    catchUp.status !== 'idle' &&
    catchUp.status !== 'unavailable'
  // The divider sits right before the first line received live (or at the tail
  // when the whole journal is still history).
  const markerIndex = useMemo(() => {
    if (startedByMe || joinedAt === null) return -1
    if (!entries.some((entry) => entry.catchup)) return -1
    const firstLive = entries.findIndex((entry) => !entry.catchup)
    return firstLive === -1 ? entries.length : firstLive
  }, [entries, startedByMe, joinedAt])

  const joinedMarker =
    markerIndex === -1 ? null : (
      <div
        key="linto-joined-marker"
        data-testid="linto-joined-marker"
        className={css({
          display: 'flex',
          alignItems: 'center',
          gap: '0.5rem',
          marginTop: '0.75rem',
          marginBottom: '0.25rem',
          '&::before, &::after': {
            content: '""',
            flex: 1,
            height: '1px',
            backgroundColor: 'greyscale.200',
          },
        })}
      >
        <Text
          variant="smNote"
          as="span"
          className={css({ flexShrink: 0, textStyle: 'sm' })}
        >
          {t('catchup.joined', {
            time: formatTime(joinedAt ?? 0, i18n.language),
          })}
        </Text>
      </div>
    )

  // The displayed language is SHARED with the caption overlay (lintoStore).
  const setDisplay = (value: string) => {
    lintoStore.displayLanguage = value
  }
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

      {showCatchUp && (
        <div
          data-testid="linto-catchup"
          data-status={catchUp.status}
          className={css({
            width: '100%',
            padding: '0.625rem 0.75rem',
            borderRadius: '4px',
            border: '1px solid',
            borderColor: 'control.border',
            backgroundColor: 'greyscale.50',
          })}
        >
          <div
            className={css({
              display: 'flex',
              alignItems: 'baseline',
              justifyContent: 'space-between',
              gap: '0.5rem',
            })}
          >
            <Text
              variant="sm"
              as="p"
              className={css({ fontWeight: 'semibold', color: 'primary.700' })}
            >
              {t('catchup.heading')}
            </Text>
            <Button
              variant="text"
              size="sm"
              data-testid="linto-catchup-refresh"
              onPress={() => refreshLintoCatchUp()}
              isDisabled={
                catchUp.status === 'loading' || catchUp.status === 'streaming'
              }
            >
              {t('catchup.refresh')}
            </Button>
          </div>
          <div data-testid="linto-catchup-summary">
            {catchUp.text ? (
              <SimpleMarkdown text={catchUp.text} />
            ) : (
              <Text variant="smNote">
                {catchUp.status === 'error'
                  ? t('catchup.error')
                  : catchUp.status === 'too_short'
                    ? t('catchup.tooShort')
                    : t('catchup.loading')}
              </Text>
            )}
          </div>
        </div>
      )}

      {entries.length === 0 ? (
        <Text variant="smNote">{t('live.empty')}</Text>
      ) : (
        <div
          className={css({
            display: 'flex',
            flexDirection: 'column',
            gap: '0.125rem',
          })}
        >
          {entries.map((entry, index) => {
            const showSpeaker =
              index === 0 || entries[index - 1].locutor !== entry.locutor
            const showTranslated = effectiveDisplay !== ORIGINAL
            const translated = translationFor(
              entry.translations,
              effectiveDisplay
            )
            // When the chosen language hasn't arrived for THIS line yet, fall
            // back to the original so it never blanks out mid-stream.
            const body = showTranslated
              ? (translated ?? entry.text)
              : entry.text
            const isTranslated = showTranslated && translated != null
            const time = formatTime(entry.receivedAt, i18n.language)
            return (
              <Fragment key={entry.id}>
                {index === markerIndex && joinedMarker}
                <div
                  data-testid="linto-turn"
                  data-speaker={entry.locutor}
                  data-partial={entry.partial ? 'true' : 'false'}
                  {...(entry.catchup ? { 'data-catchup': 'true' } : {})}
                  className={css({
                    width: '100%',
                    marginTop: showSpeaker ? '0.5rem' : 0,
                    opacity: entry.catchup ? 0.75 : 1,
                  })}
                >
                  {showSpeaker && (
                    <Text
                      variant="sm"
                      className={css({
                        fontWeight: 'semibold',
                        color: 'primary.700',
                      })}
                    >
                      {entry.locutor}
                    </Text>
                  )}
                  <div
                    className={css({
                      display: 'flex',
                      gap: '0.5rem',
                      alignItems: 'baseline',
                      opacity: entry.partial ? 0.6 : 1,
                      fontStyle: entry.partial ? 'italic' : 'normal',
                    })}
                  >
                    {time && (
                      <span
                        data-testid="linto-turn-time"
                        className={css({
                          flexShrink: 0,
                          fontVariantNumeric: 'tabular-nums',
                          fontSize: '0.6875rem',
                          color: 'greyscale.500',
                          paddingTop: '0.15rem',
                        })}
                      >
                        {time}
                      </span>
                    )}
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
                </div>
              </Fragment>
            )
          })}
          {markerIndex === entries.length && joinedMarker}
        </div>
      )}
    </div>
  )
}
