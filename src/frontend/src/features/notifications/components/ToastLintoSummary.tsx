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
            <Trans
              t={t}
              i18nKey="ownEmail"
              values={{ email }}
              components={{
                recipient: (
                  <strong
                    data-testid="linto-recipient"
                    className={css({ fontWeight: 'bold' })}
                  />
                ),
              }}
            />
          ) : (
            t('generic')
          )}
        </Text>
      </HStack>
    </StyledToastContainer>
  )
}
