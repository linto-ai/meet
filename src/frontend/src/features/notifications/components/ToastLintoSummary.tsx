import { useToast } from 'react-aria'
import { useRef } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { Text } from '@/primitives'

import { ToastProps } from './Toast'
import { StyledToastContainer } from './StyledToastContainer'
import { HStack } from '@/styled-system/jsx'
import { css } from '@/styled-system/css'

/**
 * LinTO summary-email toast (calque of ToastRecordingSaving). The branch is
 * carried on the toast content, not read from a store: `startedByMe` + `email`
 * are pushed by notifyLintoSummarySaving, so this stays decoupled from the
 * transcription-bot feature.
 */
export function ToastLintoSummary({ state, ...props }: Readonly<ToastProps>) {
  const { t } = useTranslation('transcription-bot', { keyPrefix: 'stopToast' })
  const ref = useRef(null)
  const { toastProps, contentProps } = useToast(props, state, ref)

  const startedByMe = props.toast.content.startedByMe === true
  const email =
    typeof props.toast.content.email === 'string'
      ? props.toast.content.email
      : undefined
  // Without a summary the recap carries the transcript alone: say so.
  const withSummary = props.toast.content.summary !== false
  const components = {
    recipient: (
      <strong
        data-testid="linto-recipient"
        className={css({ fontWeight: 'bold' })}
      />
    ),
  }

  return (
    <StyledToastContainer {...toastProps} ref={ref}>
      <HStack
        justify="center"
        alignItems="center"
        {...contentProps}
        padding={14}
        gap={1}
        data-testid="linto-stop-toast"
      >
        <Text
          margin={false}
          className={css({
            maxWidth: '22rem',
            wordBreak: 'break-word',
            overflowWrap: 'break-word',
            whiteSpace: 'normal',
          })}
        >
          {startedByMe && email ? (
            withSummary ? (
              <Trans
                t={t}
                i18nKey="ownEmail"
                values={{ email }}
                components={components}
              />
            ) : (
              <Trans
                t={t}
                i18nKey="ownEmailTranscript"
                values={{ email }}
                components={components}
              />
            )
          ) : withSummary ? (
            t('generic')
          ) : (
            t('genericTranscript')
          )}
        </Text>
      </HStack>
    </StyledToastContainer>
  )
}
