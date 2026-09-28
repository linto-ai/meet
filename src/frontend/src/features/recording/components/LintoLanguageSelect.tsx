import { Text } from '@/primitives'
import { css } from '@/styled-system/css'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useLintoTranscriptionLanguages } from '@/features/transcription-bot'
import { languageName } from '@/features/transcription-bot/utils/languageLabel'
import { LINTO_LANGUAGE_AUTO, recordingStore } from '@/stores/recording'

const selectClass = css({
  width: '100%',
  padding: '0.4rem',
  borderRadius: '4px',
  border: '1px solid',
  borderColor: 'control.border',
  backgroundColor: 'white',
})

/**
 * The deferred transcription's language when LinTO serves it: automatic
 * detection first, then the languages the STT services of Studio know, named
 * in the UI language. Bound to `recordingStore.lintoLanguage`, so both
 * recording tools share the choice.
 */
export const LintoLanguageSelect = ({
  isDisabled,
  testId,
}: {
  isDisabled: boolean
  testId: string
}) => {
  const { t } = useTranslation('transcription-bot', { keyPrefix: 'deferred' })
  const { i18n } = useTranslation()
  const apiRoomData = useRoomData()
  const { data: lintoLanguages } = useLintoTranscriptionLanguages(
    apiRoomData?.livekit?.room,
    apiRoomData?.livekit?.token
  )
  const { lintoLanguage } = useSnapshot(recordingStore)
  const languageOptions = (lintoLanguages ?? [])
    .filter((code) => code !== '*')
    .map((code) => ({ code, label: languageName(code, i18n.language) }))
    .sort((a, b) => a.label.localeCompare(b.label, i18n.language))

  return (
    <label className={css({ width: '100%' })}>
      <Text variant="sm" as="span">
        {t('language')}
      </Text>
      <select
        data-testid={testId}
        className={selectClass}
        value={lintoLanguage}
        disabled={isDisabled}
        onChange={(e) => {
          recordingStore.lintoLanguage = e.target.value
        }}
      >
        <option value={LINTO_LANGUAGE_AUTO}>{t('languageAuto')}</option>
        {languageOptions.map((option) => (
          <option key={option.code} value={option.code}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}
