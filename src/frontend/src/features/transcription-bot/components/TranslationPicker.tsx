import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { RiCheckLine } from '@remixicon/react'
import { css } from '@/styled-system/css'
import { Text } from '@/primitives'
import { useConfig } from '@/api/useConfig'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { lintoStore } from '../store/lintoStore'
import { useLintoBotProfiles } from '../api/lintoBotApi'
import { languageName } from '../utils/languageLabel'

// Baseline translation targets, offered when the pinned profile advertises none.
const BASE_TRANSLATION_LANGS = ['fr', 'en', 'de', 'es']

// At most this many suggested chips (the Studio mobile app's figure).
const MAX_SUGGESTIONS = 6

const baseCode = (code: string) => code.split('-')[0].toLowerCase()

const chipClass = (on: boolean, disabled?: boolean) =>
  css({
    display: 'inline-flex',
    alignItems: 'center',
    gap: '0.25rem',
    minHeight: '2rem',
    paddingX: '0.75rem',
    borderRadius: '999px',
    border: '1px solid',
    borderColor: on ? 'primary.500' : 'control.border',
    backgroundColor: on ? 'primary.100' : 'white',
    color: on ? 'primary.800' : 'inherit',
    fontSize: '0.8125rem',
    fontWeight: on ? 'semibold' : 'medium',
    cursor: disabled ? 'default' : 'pointer',
    _hover: disabled ? {} : { backgroundColor: on ? 'primary.200' : 'gray.50' },
  })

const selectClass = css({
  width: '100%',
  padding: '0.4rem',
  borderRadius: '4px',
  border: '1px solid',
  borderColor: 'control.border',
  backgroundColor: 'white',
})

interface TranslationPickerProps {
  isDisabled?: boolean
}

/**
 * Live translation targets, as the Studio mobile app picks them
 * (`mobile/components/live/TranslationPicker.vue`): a "None" chip, a few
 * suggested languages as chips — the ones the ASR profile can hear that are
 * also available targets, at most six — plus whatever is already selected,
 * and a select for every other available target. Names come from
 * `Intl.DisplayNames` in the UI language, sorted.
 *
 * No source language: the spoken language is detected automatically.
 */
export const TranslationPicker = ({ isDisabled }: TranslationPickerProps) => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'lintoBot.translations',
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
  // The pinned profile bounds the targets (what the ASR can actually translate
  // to); fall back to the first profile, then to a baseline set.
  const profile = useMemo(
    () =>
      (pinnedProfileId && profiles.find((p) => p.id === pinnedProfileId)) ||
      profiles[0],
    [profiles, pinnedProfileId]
  )

  // Every available target, named and sorted (listProfileTranslations).
  const options = useMemo(() => {
    const fromProfile = profile?.translations ?? []
    const codes = Array.from(
      new Set(fromProfile.length > 0 ? fromProfile : BASE_TRANSLATION_LANGS)
    )
    return codes
      .map((code) => ({ code, label: languageName(code, locale) }))
      .sort((a, b) => a.label.localeCompare(b.label, locale))
  }, [profile, locale])

  // Suggestions: the languages the profile can hear ∩ the targets, ≤ 6
  // (suggestTranslationTargets).
  const suggestions = useMemo(() => {
    const spoken = new Set((profile?.languages ?? []).map(baseCode))
    return options
      .filter((option) => spoken.has(baseCode(option.code)))
      .slice(0, MAX_SUGGESTIONS)
  }, [profile, options])

  // Chips = suggestions + the selected languages that are not suggested.
  const shownChips = useMemo(() => {
    const suggested = new Set(suggestions.map((option) => option.code))
    const extra = options.filter(
      (option) =>
        selectedTranslations.includes(option.code) &&
        !suggested.has(option.code)
    )
    return [...suggestions, ...extra]
  }, [suggestions, options, selectedTranslations])

  const remaining = useMemo(() => {
    const shown = new Set(shownChips.map((option) => option.code))
    return options.filter((option) => !shown.has(option.code))
  }, [options, shownChips])

  const isSelected = (code: string) => selectedTranslations.includes(code)
  const toggle = (code: string) => {
    const current = lintoStore.selectedTranslations
    lintoStore.selectedTranslations = current.includes(code)
      ? current.filter((item) => item !== code)
      : [...current, code]
  }
  const addFromSelect = (code: string) => {
    if (code && !isSelected(code)) {
      lintoStore.selectedTranslations = [
        ...lintoStore.selectedTranslations,
        code,
      ]
    }
  }

  const none = selectedTranslations.length === 0

  return (
    <div
      data-testid="linto-target-langs"
      className={css({
        width: '100%',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.5rem',
      })}
    >
      <Text variant="sm" as="span">
        {t('label')}
      </Text>
      <div
        role="group"
        aria-label={t('label')}
        className={css({
          display: 'flex',
          flexWrap: 'wrap',
          gap: '0.375rem',
        })}
      >
        <button
          type="button"
          data-testid="linto-target-chip-none"
          aria-pressed={none}
          disabled={isDisabled}
          onClick={() => {
            lintoStore.selectedTranslations = []
          }}
          className={chipClass(none, isDisabled)}
        >
          {t('none')}
        </button>
        {shownChips.map((option) => {
          const on = isSelected(option.code)
          return (
            <button
              key={option.code}
              type="button"
              data-testid={`linto-target-chip-${option.code}`}
              aria-pressed={on}
              disabled={isDisabled}
              onClick={() => toggle(option.code)}
              className={chipClass(on, isDisabled)}
            >
              {on && <RiCheckLine size={14} aria-hidden="true" />}
              {option.label}
            </button>
          )
        })}
      </div>
      {remaining.length > 0 && (
        <label className={css({ width: '100%' })}>
          <Text variant="sm" as="span">
            {t('other')}
          </Text>
          <select
            data-testid="linto-target-more"
            className={selectClass}
            value=""
            disabled={isDisabled}
            onChange={(e) => {
              addFromSelect(e.target.value)
              e.target.value = ''
            }}
          >
            <option value="" disabled>
              {t('otherPlaceholder')}
            </option>
            {remaining.map((option) => (
              <option key={option.code} value={option.code}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      )}
      {options.length === 0 && (
        <Text variant="smNote">{t('noneAvailable')}</Text>
      )}
    </div>
  )
}
