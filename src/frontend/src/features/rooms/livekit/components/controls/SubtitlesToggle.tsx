import { useTranslation } from 'react-i18next'
import { RiClosedCaptioningLine } from '@remixicon/react'
import { ToggleButton } from '@/primitives'
import { css } from '@/styled-system/css'
import { useSubtitles } from '@/features/subtitle/hooks/useSubtitles'
import { useAreSubtitlesAvailable } from '@/features/subtitle/hooks/useAreSubtitlesAvailable'
import { CaptionsLanguageMenu } from '@/features/subtitle/component/CaptionsLanguageMenu'
import { useLintoDisplayLanguages } from '@/features/transcription-bot/hooks/useLintoDisplayLanguages'

export const SubtitlesToggle = () => {
  const { t } = useTranslation('rooms', { keyPrefix: 'controls.subtitles' })
  const { t: tLinto } = useTranslation('transcription-bot', {
    keyPrefix: 'captions',
  })
  const {
    areSubtitlesOpen,
    toggleSubtitles,
    areSubtitlesPending,
    isLintoActive,
  } = useSubtitles()
  const tooltipLabel = areSubtitlesOpen ? 'open' : 'closed'
  const areSubtitlesAvailable = useAreSubtitlesAvailable()
  const { languages } = useLintoDisplayLanguages()
  // Same condition as CaptionsLanguageMenu rendering (it returns null otherwise).
  const hasLanguageMenu = isLintoActive && languages.length > 0

  if (!areSubtitlesAvailable) return null

  // While LinTO transcribes the room, the CC button shows the captions it
  // publishes — say so (badge + label) instead of the generic wording.
  const label = isLintoActive ? tLinto('label') : t(tooltipLabel)

  return (
    <div className={css({ display: 'flex', gap: '1px' })}>
      <div
        className={css({
          position: 'relative',
          display: 'inline-block',
        })}
      >
        <ToggleButton
          square
          groupPosition={hasLanguageMenu ? 'left' : undefined}
          variant="primaryDark"
          aria-label={label}
          tooltip={label}
          isSelected={areSubtitlesOpen}
          isDisabled={areSubtitlesPending}
          onPress={toggleSubtitles}
          data-attr={`controls-subtitles-${tooltipLabel}`}
        >
          <RiClosedCaptioningLine />
        </ToggleButton>
        {isLintoActive && (
          <span
            data-testid="cc-linto-live"
            aria-hidden="true"
            className={css({
              position: 'absolute',
              top: '-0.35rem',
              right: '-0.35rem',
              paddingX: '0.3rem',
              borderRadius: '999px',
              fontSize: '0.6rem',
              lineHeight: '1rem',
              fontWeight: 'bold',
              backgroundColor: 'success.700',
              color: 'white',
              pointerEvents: 'none',
            })}
          >
            {tLinto('available')}
          </span>
        )}
      </div>
      <CaptionsLanguageMenu isLintoActive={isLintoActive} />
    </div>
  )
}
