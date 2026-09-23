import { css } from '@/styled-system/css'
import { VStack } from '@/styled-system/jsx'
import { Text } from '@/primitives'
import { Checkbox } from '@/primitives/Checkbox'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { useMemo, useState } from 'react'
import { useConfig } from '@/api/useConfig'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { lintoStore } from '../store/lintoStore'
import { useLintoBotProfiles } from '../api/lintoBotApi'
import { languageLabel } from '../utils/languageLabel'

const fieldClass = css({
  width: '100%',
  padding: '0.5rem',
  borderRadius: '4px',
  border: '1px solid',
  borderColor: 'control.border',
  backgroundColor: 'white',
})

// Baseline translation targets, offered when the pinned profile advertises none.
const BASE_TRANSLATION_LANGS = ['fr', 'en', 'de', 'es']

interface LintoSettingsProps {
  isDisabled?: boolean
}

/**
 * The live-translation targets, shown under the "translate in real time"
 * checkbox once it is ticked. Deliberately minimal for the end user:
 * - NO source-language choice (always automatic).
 * - NO ASR profile choice (pinned by ops via LINTO_STUDIO_DEFAULT_PROFILE_ID,
 *   invisible to the user).
 */
export const LintoSettings = ({ isDisabled }: LintoSettingsProps) => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'lintoBot.settings',
  })
  const locale = i18n.language
  const { selectedTranslations } = useSnapshot(lintoStore)

  const apiRoomData = useRoomData()
  const roomId = apiRoomData?.livekit?.room
  const token = apiRoomData?.livekit?.token
  const { data } = useLintoBotProfiles(roomId, token)
  const { data: configData } = useConfig()
  const pinnedProfileId = configData?.linto?.default_profile_id

  const profiles = useMemo(() => data?.profiles ?? [], [data])
  // The pinned profile bounds the translation targets (what the ASR can actually
  // translate to); fall back to the first profile, then to a baseline set.
  const effectiveProfile = useMemo(
    () =>
      (pinnedProfileId && profiles.find((p) => p.id === pinnedProfileId)) ||
      profiles[0],
    [profiles, pinnedProfileId]
  )
  const translationLangs = useMemo(() => {
    const fromProfile = effectiveProfile?.translations ?? []
    return Array.from(
      new Set(fromProfile.length > 0 ? fromProfile : BASE_TRANSLATION_LANGS)
    )
  }, [effectiveProfile])

  const toggleTranslation = (lang: string, selected: boolean) => {
    const current = lintoStore.selectedTranslations
    if (selected) {
      if (!current.includes(lang))
        lintoStore.selectedTranslations = [...current, lang]
    } else {
      lintoStore.selectedTranslations = current.filter((l) => l !== lang)
    }
  }

  // Searchable picker: bare 2-letter codes are cryptic, so show full localized
  // names and filter by typed text (name or code). Sorted by name.
  const [query, setQuery] = useState('')
  const sortedLangs = useMemo(
    () =>
      [...translationLangs].sort((a, b) =>
        languageLabel(a, locale).localeCompare(languageLabel(b, locale))
      ),
    [translationLangs, locale]
  )
  const filteredLangs = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return sortedLangs
    return sortedLangs.filter(
      (lang) =>
        lang.toLowerCase().includes(q) ||
        languageLabel(lang, locale).toLowerCase().includes(q)
    )
  }, [sortedLangs, query, locale])
  const selectedChips = useMemo(
    () => sortedLangs.filter((lang) => selectedTranslations.includes(lang)),
    [sortedLangs, selectedTranslations]
  )

  return (
    <VStack gap={0.75} width="100%" alignItems="start">
      <div
        data-testid="linto-target-langs"
        className={css({ width: '100%', paddingLeft: '1.625rem' })}
        role="group"
        aria-label={t('translations')}
      >
        {/* Selected languages as removable chips — stay visible even when
            filtered out of the list below. */}
        {selectedChips.length > 0 && (
          <div
            className={css({
              display: 'flex',
              flexWrap: 'wrap',
              gap: '0.375rem',
              marginTop: '0.375rem',
            })}
          >
            {selectedChips.map((lang) => (
              <button
                key={lang}
                type="button"
                data-testid={`linto-target-chip-${lang}`}
                aria-label={t('translationsRemove', {
                  lang: languageLabel(lang, locale),
                })}
                disabled={isDisabled}
                onClick={() => toggleTranslation(lang, false)}
                className={css({
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '0.25rem',
                  paddingY: '0.125rem',
                  paddingX: '0.5rem',
                  borderRadius: '999px',
                  fontSize: '0.8125rem',
                  backgroundColor: 'primary.100',
                  color: 'primary.800',
                  cursor: isDisabled ? 'default' : 'pointer',
                  _hover: { backgroundColor: 'primary.200' },
                })}
              >
                {languageLabel(lang, locale)}
                <span aria-hidden="true">×</span>
              </button>
            ))}
          </div>
        )}

        {/* Type-to-filter search over the full localized names. */}
        <input
          type="text"
          data-testid="linto-target-search"
          className={fieldClass}
          placeholder={t('translationsSearch')}
          value={query}
          disabled={isDisabled}
          onChange={(e) => setQuery(e.target.value)}
          aria-label={t('translationsSearch')}
        />

        {/* Scrollable checkbox list — caps the height so the targets never push
            the Start button off-screen. */}
        <div
          className={css({
            marginTop: '0.375rem',
            maxHeight: '11rem',
            overflowY: 'auto',
            border: '1px solid',
            borderColor: 'control.border',
            borderRadius: '4px',
            padding: '0.5rem',
            display: 'flex',
            flexDirection: 'column',
            gap: '0.5rem',
          })}
        >
          {filteredLangs.length === 0 ? (
            <Text variant="smNote">{t('translationsNone')}</Text>
          ) : (
            filteredLangs.map((lang) => (
              <span key={lang} data-testid={`linto-target-lang-${lang}`}>
                <Checkbox
                  size="sm"
                  isSelected={selectedTranslations.includes(lang)}
                  onChange={(value) => toggleTranslation(lang, value)}
                  isDisabled={isDisabled}
                >
                  <Text variant="sm">{languageLabel(lang, locale)}</Text>
                </Checkbox>
              </span>
            ))
          )}
        </div>

        {selectedChips.length > 0 && (
          <Text variant="smNote" className={css({ marginTop: '0.25rem' })}>
            {t('translationsCount', { count: selectedChips.length })}
          </Text>
        )}
      </div>
    </VStack>
  )
}
