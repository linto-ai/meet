import { useTranslation } from 'react-i18next'
import { RiArrowUpSLine } from '@remixicon/react'
import { Button, Menu, MenuList } from '@/primitives'
import { useLintoDisplayLanguages } from '@/features/transcription-bot/hooks/useLintoDisplayLanguages'
import {
  languageName,
  ORIGINAL,
} from '@/features/transcription-bot/utils/languageLabel'

/**
 * Chevron glued to the CC button: picks the language the LinTO live captions
 * are DISPLAYED in (original or one of the live translations). The choice is
 * shared with the LinTO panel selector and never opens or closes the overlay.
 * Only rendered while LinTO transcribes the room and translations exist.
 */
export const CaptionsLanguageMenu = ({
  isLintoActive,
}: {
  isLintoActive: boolean
}) => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'captions',
  })
  const { languages, effective, setDisplay } = useLintoDisplayLanguages()

  if (!isLintoActive || languages.length === 0) return null

  const label = t('languageMenu')
  const items = [ORIGINAL, ...languages].map((code) => ({
    value: code,
    label:
      code === ORIGINAL ? t('original') : languageName(code, i18n.language),
    itemProps: {
      'data-testid': 'cc-language-option',
      'data-lang': code,
    },
  }))

  return (
    <Menu variant="dark" placement="top">
      <Button
        square
        groupPosition="right"
        variant="primaryDark"
        aria-label={label}
        tooltip={label}
        data-testid="cc-language-menu"
      >
        <RiArrowUpSLine />
      </Button>
      <MenuList
        variant="dark"
        aria-label={label}
        items={items}
        selectedItem={effective}
        onAction={(value) => setDisplay(String(value))}
      />
    </Menu>
  )
}
