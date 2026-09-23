import { useToast } from 'react-aria'
import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Button as RACButton } from 'react-aria-components'
import { RiSparklingLine } from '@remixicon/react'
import { Text } from '@/primitives'
import { css } from '@/styled-system/css'
import { useSidePanel } from '@/features/rooms/livekit/hooks/useSidePanel'
import { ToastProps } from './Toast'
import { StyledToastContainer } from './StyledToastContainer'

/**
 * "What was said before you arrived is ready" toast (fork, calque of
 * ToastMessageReceived): a discreet, pressable notice for a late joiner whose
 * live transcription panel is closed. Pressing it opens the panel; it closes
 * by itself as soon as the panel shows.
 */
export function ToastLintoCatchUp({ state, ...props }: Readonly<ToastProps>) {
  const { t } = useTranslation('transcription-bot', {
    keyPrefix: 'catchupToast',
  })
  const ref = useRef(null)
  const { toastProps } = useToast(props, state, ref)
  const toast = props.toast
  const { isLintoOpen } = useSidePanel()
  const onOpen =
    typeof toast.content.onOpen === 'function' ? toast.content.onOpen : null

  useEffect(() => {
    if (isLintoOpen) state.close(toast.key)
  }, [isLintoOpen, toast, state])

  if (isLintoOpen) return null

  return (
    <StyledToastContainer {...toastProps} ref={ref}>
      <RACButton
        data-testid="linto-catchup-toast"
        aria-label={t('open')}
        onPress={() => {
          onOpen?.()
          state.close(toast.key)
        }}
      >
        <div
          className={css({
            display: 'flex',
            flexDirection: 'row',
            alignItems: 'center',
            padding: '14px',
            gap: '0.75rem',
            textAlign: 'start',
            width: '250px',
            md: { width: '350px' },
          })}
        >
          <RiSparklingLine
            size={20}
            className={css({ color: 'primary.300', flexShrink: 0 })}
            aria-hidden="true"
          />
          <Text margin={false} centered={false} wrap={'pretty'} fullWidth>
            {t('body')}
          </Text>
        </div>
      </RACButton>
    </StyledToastContainer>
  )
}
