import { css } from '@/styled-system/css'
import { Button, Div, Text } from '@/primitives'
import { useTranslation } from 'react-i18next'
import { useSidePanel } from '@/features/rooms/livekit/hooks/useSidePanel'
import { useCatchUpView } from '../hooks/useCatchUpView'
import { retryAudioCatchUp } from '../hooks/useLintoAudioCatchUp'
import { retryLintoCatchUp } from '../hooks/useLintoCatchUp'
import type { CatchUpView } from '../utils/catchUpView'
import { SimpleMarkdown } from './SimpleMarkdown'

// Wall-clock HH:MM.
const formatTime = (ms: number | null, locale: string): string => {
  if (!ms) return ''
  try {
    return new Date(ms).toLocaleTimeString(locale, {
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
}

// The note under a brief that could not be updated: why, and what it covers.
const staleKey = (view: CatchUpView) =>
  view.failure === 'gap' ||
  view.failure === 'stopped' ||
  view.failure === 'interrupted'
    ? view.failure
    : 'generic'

/**
 * "Before you arrived" (fork): the brief of what a late joiner missed, in a
 * side panel of its own, from a live run or from the recording's audio. It
 * opens by itself at arrival and shows the wait (the steps and an estimate for
 * the audio, over the previous brief if any), then the brief, or why there is
 * none. No tool control nor login prompt: a guest reads it as is. Closing the
 * panel keeps the brief, which the Tools list offers again.
 */
export const CatchUpPanel = () => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'lintoBot',
  })
  const { openLinto } = useSidePanel()
  const view = useCatchUpView()

  if (!view) {
    return (
      <Div padding="0 1.5rem" data-testid="linto-catchup-empty">
        <Text variant="note">{t('catchup.empty')}</Text>
      </Div>
    )
  }

  const eta = (() => {
    if (view.reconnecting) return t('catchup.reconnecting')
    if (view.overdue) return t('audioCatchup.overdue')
    if (!view.etaS) return ''
    const [low, high] = view.etaS
    if (high < 60) return t('audioCatchup.etaUnderMinute')
    const min = Math.max(1, Math.round(low / 60))
    const max = Math.max(min, Math.ceil(high / 60))
    return min === max
      ? t('audioCatchup.etaMinutes', { count: min })
      : t('audioCatchup.etaRange', { min, max })
  })()

  return (
    <div
      data-testid="linto-catchup"
      data-source={view.source}
      data-status={view.status}
      className={css({
        display: 'flex',
        flexDirection: 'column',
        gap: '0.75rem',
        flexGrow: 1,
        minHeight: 0,
        overflowY: 'auto',
        padding: '0 1.5rem 1rem',
      })}
    >
      <Text variant="note" as="p" data-testid="linto-catchup-source">
        {t(`catchup.source.${view.source}`)}
      </Text>

      <div aria-live="polite" data-testid="linto-catchup-progress">
        {(view.phase === 'preparing' || view.step) && (
          <div
            className={css({
              display: 'flex',
              flexDirection: 'column',
              gap: '0.375rem',
            })}
          >
            <Text variant="sm" as="p">
              {view.phase === 'text'
                ? t('audioCatchup.updating')
                : view.step
                  ? t(`audioCatchup.step.${view.step}`)
                  : t('catchup.loading')}
              {eta ? ` · ${eta}` : ''}
            </Text>
            {view.progress !== null && (
              <div
                role="progressbar"
                aria-label={t('audioCatchup.progress')}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(view.progress * 100)}
                className={css({
                  width: '100%',
                  height: '4px',
                  borderRadius: '2px',
                  backgroundColor: 'greyscale.200',
                  overflow: 'hidden',
                })}
              >
                <div
                  className={css({
                    height: '100%',
                    backgroundColor: 'primary.500',
                    transition: 'width 0.6s ease',
                  })}
                  style={{
                    width: `${Math.max(4, Math.round(view.progress * 100))}%`,
                  }}
                />
              </div>
            )}
            {view.overdue && !view.reconnecting && (
              <Text variant="smNote" as="p">
                {t('audioCatchup.overdueHint')}
              </Text>
            )}
          </div>
        )}
        {view.phase === 'too_short' && (
          <Text variant="sm" as="p" data-testid="linto-catchup-too-short">
            {t('audioCatchup.tooShort')}
          </Text>
        )}
        {view.phase === 'failed' && (
          <Text
            variant="sm"
            as="p"
            data-testid="linto-catchup-failed"
            data-failure={view.failure ?? 'generic'}
          >
            {t(`catchup.failure.${view.failure ?? 'generic'}`)}
          </Text>
        )}
      </div>

      {view.phase === 'text' && (
        <div data-testid="linto-catchup-summary">
          <SimpleMarkdown text={view.text} />
        </div>
      )}
      {view.phase === 'text' && view.stale && (
        <Text
          variant="sm"
          as="p"
          data-testid="linto-catchup-stale"
          data-failure={view.failure ?? 'generic'}
        >
          {t(`catchup.stale.${staleKey(view)}`, {
            time: formatTime(view.coveredUntil, i18n.language),
          })}
        </Text>
      )}
      {view.canRetry && (view.phase === 'failed' || view.stale) && (
        <div>
          <Button
            variant="secondary"
            size="sm"
            data-testid="linto-catchup-retry"
            onPress={() =>
              view.source === 'live' ? retryLintoCatchUp() : retryAudioCatchUp()
            }
          >
            {t('catchup.retry')}
          </Button>
        </div>
      )}
      {view.phase === 'text' &&
        !view.stale &&
        (view.coveredUntil || view.joinedAt) && (
          <Text variant="smNote" as="p" data-testid="linto-catchup-covered">
            {view.coveredUntil
              ? t('audioCatchup.coveredUntil', {
                  time: formatTime(view.coveredUntil, i18n.language),
                })
              : t('catchup.joined', {
                  time: formatTime(view.joinedAt, i18n.language),
                })}
          </Text>
        )}
      {view.phase === 'text' && (
        <Text variant="smNote" as="p">
          {t('catchup.disclaimer')}
        </Text>
      )}

      {view.source === 'live' && (
        <div className={css({ marginTop: 'auto', paddingTop: '0.5rem' })}>
          <Button
            variant="secondaryText"
            size="sm"
            data-testid="linto-catchup-open-live"
            onPress={() => openLinto()}
          >
            {t('catchup.openLive')}
          </Button>
        </div>
      )}
    </div>
  )
}
