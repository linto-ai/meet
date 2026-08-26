import { css } from '@/styled-system/css'
import { VStack } from '@/styled-system/jsx'
import { Text } from '@/primitives'
import { Checkbox } from '@/primitives/Checkbox'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { useMemo, useState } from 'react'
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

const skeletonClass = css({
  width: '100%',
  height: '2.25rem',
  borderRadius: '4px',
  backgroundColor: 'greyscale.100',
  animationName: 'pulse',
  animationDuration: '1.5s',
  animationTimingFunction: 'ease-in-out',
  animationIterationCount: 'infinite',
})

// Source ASR language. The backend provides a default, so "auto" is non-blocking.
const LANGUAGE_OPTIONS = ['', 'fr', 'en']

// Baseline translation targets, always offered; merged with the chosen profile's
// advertised `translations` when available.
const BASE_TRANSLATION_LANGS = ['fr', 'en', 'de', 'es']

interface LintoSettingsProps {
  isDisabled?: boolean
}

export const LintoSettings = ({ isDisabled }: LintoSettingsProps) => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'lintoBot.settings',
  })
  const locale = i18n.language
  const { selectedLanguage, selectedProfile, selectedTranslations } =
    useSnapshot(lintoStore)

  const apiRoomData = useRoomData()
  const roomId = apiRoomData?.livekit?.room
  const token = apiRoomData?.livekit?.token
  const { data, isLoading } = useLintoBotProfiles(roomId, token)

  const profiles = useMemo(() => data?.profiles ?? [], [data])
  const hasDefault = data?.hasDefault ?? false
  // Empty state: the org has no profile and no default → nothing selectable.
  const noProfiles = !isLoading && profiles.length === 0 && !hasDefault
  // Exactly one profile → used silently, no dropdown.
  const singleProfile = profiles.length === 1
  // Only offer a real choice when there are at least two profiles.
  const showProfileSelect = profiles.length >= 2

  // The profile bounding the translation targets: the explicit pick, or the
  // single silent profile when there is exactly one.
  const effectiveProfile = useMemo(() => {
    if (singleProfile) return profiles[0]
    return profiles.find((p) => p.id === selectedProfile)
  }, [singleProfile, profiles, selectedProfile])

  // Bound the translation multi-select to the chosen profile's targets when it
  // advertises any; otherwise fall back to the baseline set.
  const translationLangs = useMemo(() => {
    const fromProfile = effectiveProfile?.translations ?? []
    const merged = fromProfile.length > 0 ? fromProfile : BASE_TRANSLATION_LANGS
    return Array.from(new Set(merged))
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

  // Searchable picker: bare 2-letter codes are cryptic and the list can reach
  // ~25 targets, so we show full localized names and filter by typed text
  // (matching either the name or the code). Sorted by name for scannability.
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
  // Selected chips: keep them visible even if filtered out of the list below.
  const selectedChips = useMemo(
    () => sortedLangs.filter((lang) => selectedTranslations.includes(lang)),
    [sortedLangs, selectedTranslations]
  )

  return (
    <VStack gap={0.75} width="100%" marginBottom={20} alignItems="start">
      <label className={css({ width: '100%' })}>
        <Text variant="sm">{t('language')}</Text>
        <select
          data-testid="linto-language"
          className={fieldClass}
          disabled={isDisabled}
          value={selectedLanguage ?? ''}
          onChange={(e) => {
            lintoStore.selectedLanguage = e.target.value || undefined
          }}
        >
          {LANGUAGE_OPTIONS.map((lang) => (
            <option key={lang || 'auto'} value={lang}>
              {lang ? lang.toUpperCase() : t('languageAuto')}
            </option>
          ))}
        </select>
      </label>

      {isLoading ? (
        <div className={css({ width: '100%' })}>
          <Text variant="sm">{t('asrProfile')}</Text>
          <div className={skeletonClass} aria-hidden="true" />
        </div>
      ) : noProfiles ? (
        <div
          data-testid="linto-no-profile"
          className={css({ width: '100%' })}
          role="group"
          aria-label={t('asrProfile')}
        >
          <Text variant="sm">{t('asrProfile')}</Text>
          <select
            data-testid="linto-asr-profile"
            className={fieldClass}
            disabled
            value=""
          >
            <option value="">{t('asrProfileEmpty')}</option>
          </select>
          <Text variant="smNote" className={css({ marginTop: '0.25rem' })}>
            {t('asrProfileAdminNote')}
          </Text>
        </div>
      ) : (
        showProfileSelect && (
          <label className={css({ width: '100%' })}>
            <Text variant="sm">{t('asrProfile')}</Text>
            <select
              data-testid="linto-asr-profile"
              className={fieldClass}
              disabled={isDisabled}
              value={selectedProfile ?? ''}
              onChange={(e) => {
                lintoStore.selectedProfile = e.target.value || undefined
                // Drop translations no longer offered by the newly chosen profile.
                const next = profiles.find((p) => p.id === e.target.value)
                const allowed = next?.translations ?? []
                if (allowed.length > 0) {
                  lintoStore.selectedTranslations =
                    lintoStore.selectedTranslations.filter((l) =>
                      allowed.includes(l)
                    )
                }
              }}
            >
              <option value="">{t('asrProfileAuto')}</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )
      )}

      {!noProfiles && (
        <div
          data-testid="linto-target-langs"
          className={css({ width: '100%' })}
          role="group"
          aria-label={t('translations')}
        >
          <Text variant="sm">{t('translations')}</Text>

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
            style={{ marginTop: '0.375rem' }}
          />

          {/* Scrollable checkbox list — caps the height so 25 targets never
              push the Start button off-screen. */}
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
      )}
    </VStack>
  )
}
