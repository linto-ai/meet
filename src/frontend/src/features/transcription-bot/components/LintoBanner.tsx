import { css } from '@/styled-system/css'
import { useTranslation } from 'react-i18next'
import { Icon, Text } from '@/primitives'
import { Button as RACButton } from 'react-aria-components'
import { useSidePanel } from '@/features/rooms/livekit/hooks/useSidePanel'
import { useLintoStatus } from '../hooks/useLintoStatus'

/**
 * Ambient "Transcription en cours" banner shown to EVERY participant while a
 * LinTO bot runs — independent of whether the panel is open.
 *
 * It reads the dedicated LiveKit room-metadata flag (pushed by the backend at
 * start, cleared at stop), NOT `recording_mode`: that keeps it clear of the
 * recording egress banner (RecordingStateToast). Clicking it opens the panel.
 */
export const LintoBanner = () => {
  const { t } = useTranslation('transcription-bot', { keyPrefix: 'banner' })
  const { active } = useLintoStatus()
  const { openLinto } = useSidePanel()

  if (!active) return null

  return (
    <div
      data-testid="linto-banner"
      className={css({
        display: 'flex',
        position: 'fixed',
        // Stacked just under the recording egress banner so both can coexist
        // when the `record` add-on is also on.
        top: '48px',
        left: '10px',
        paddingY: '0.25rem',
        paddingX: '0.75rem',
        backgroundColor: 'danger.700',
        borderColor: 'white',
        border: '1px solid',
        color: 'white',
        borderRadius: '4px',
        gap: '0.5rem',
        alignItems: 'center',
        zIndex: 1,
      })}
    >
      <span
        aria-hidden="true"
        className={css({
          width: '0.5rem',
          height: '0.5rem',
          borderRadius: '50%',
          backgroundColor: 'white',
          flexShrink: 0,
        })}
      />
      <Icon name="speech_to_text" />
      <RACButton
        onPress={openLinto}
        className={css({
          textStyle: 'sm !important',
          fontWeight: '500 !important',
          cursor: 'pointer',
        })}
      >
        <Text variant="sm" className={css({ fontWeight: '500 !important' })}>
          {t('inProgress')}
        </Text>
      </RACButton>
    </div>
  )
}
