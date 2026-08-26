import { useTranslation } from 'react-i18next'
import { RiClosedCaptioningLine } from '@remixicon/react'
import { ToggleButton } from '@/primitives'
import { css } from '@/styled-system/css'
import { useSubtitles } from '@/features/subtitle/hooks/useSubtitles'
import { useAreSubtitlesAvailable } from '@/features/subtitle/hooks/useAreSubtitlesAvailable'

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

  if (!areSubtitlesAvailable) return null

  // While LinTO transcribes the room, the CC button shows the captions it
  // publishes — say so (badge + label) instead of the generic wording.
  const label = isLintoActive ? tLinto('label') : t(tooltipLabel)

  return (
    <div
      className={css({
        position: 'relative',
        display: 'inline-block',
      })}
    >
      <ToggleButton
        square
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
          LinTO
        </span>
      )}
    </div>
  )
}
