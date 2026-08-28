import { useEffect } from 'react'
import { Button, Div, H, Text } from '@/primitives'
import { css } from '@/styled-system/css'
import { HStack, VStack } from '@/styled-system/jsx'
import { Checkbox } from '@/primitives/Checkbox'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { ApiError } from '@/api/ApiError'
import { useConfig } from '@/api/useConfig'
import { useUser } from '@/features/auth/api/useUser'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useIsAdminOrOwner } from '@/features/rooms/livekit/hooks/useIsAdminOrOwner'
import {
  RecordingMode,
  useHasRecordingAccess,
  useHasFeatureWithoutAdminRights,
} from '@/features/recording'
import { NoAccessView } from '@/features/recording/components/NoAccessView'
import { FeatureFlags } from '@/features/analytics/enums'
import {
  NotificationType,
  notifyLintoSummarySaving,
  useNotifyParticipants,
} from '@/features/notifications'
import { lintoStore, resetLintoRun } from '../store/lintoStore'
import { clearTranscript } from '../store/transcriptStore'
import { useLintoConfig } from '../hooks/useLintoConfig'
import {
  useLintoBotProfiles,
  useStartLintoLive,
  useStopLintoLive,
} from '../api/lintoBotApi'
import { LintoSettings } from './LintoSettings'
import { LiveTranscript } from './LiveTranscript'

// Suppress the room-wide state sync from clobbering optimistic state for a
// short window after a local start/stop (the metadata roundtrip lags the
// mutation result).
const LOCAL_ACTION_GUARD_MS = 6000

/** Pull the machine code + human message out of an ApiError body without `any`. */
const parseApiError = (err: ApiError): { code?: string; message?: string } => {
  const body = err.body
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>
    return {
      code: typeof b.code === 'string' ? b.code : undefined,
      message: typeof b.error === 'string' ? b.error : undefined,
    }
  }
  return {}
}

export const LintoSidePanel = () => {
  const { t } = useTranslation('transcription-bot', { keyPrefix: 'lintoBot' })

  const { enabled } = useLintoConfig()
  const { running, summary, record, error } = useSnapshot(lintoStore)

  const apiRoomData = useRoomData()
  const roomId = apiRoomData?.livekit?.room
  const roomSlug = apiRoomData?.slug
  const token = apiRoomData?.livekit?.token

  const { user } = useUser()
  const { data: configData } = useConfig()
  const lintoConfig = configData?.linto
  const isAdminOrOwner = useIsAdminOrOwner()
  const { notifyParticipants } = useNotifyParticipants()

  // Permission gating (mirrors the legacy recording machinery).
  const hasTranscriptNoAccess = useHasFeatureWithoutAdminRights(
    RecordingMode.Transcript,
    FeatureFlags.Transcript
  )
  const hasScreenRecordingAccess = useHasRecordingAccess(
    RecordingMode.ScreenRecording,
    FeatureFlags.ScreenRecording
  )

  // A participant without screen-recording rights must never start a recording —
  // force the add-on OFF so a forged config can't carry it.
  useEffect(() => {
    if (!hasScreenRecordingAccess && lintoStore.record) {
      lintoStore.record = false
    }
  }, [hasScreenRecordingAccess])

  // The empty state (no profile AND no default) is the only thing that blocks
  // starting — live transcription is otherwise always possible.
  const { data: profilesData } = useLintoBotProfiles(roomId, token)
  // An ops-pinned profile (LINTO_STUDIO_DEFAULT_PROFILE_ID) always makes the
  // feature usable; only an org with genuinely no profile blocks Start.
  const noProfiles =
    !lintoConfig?.default_profile_id &&
    !!profilesData &&
    profilesData.profiles.length === 0 &&
    !profilesData.hasDefault

  const { mutateAsync: startLintoLive, isPending: isPendingToStart } =
    useStartLintoLive({
      onSuccess: (run) => {
        lintoStore.running = true
        lintoStore.sessionId = run.sessionId
        lintoStore.channelId = run.channelId
        lintoStore.botId = run.botId ?? undefined
        lintoStore.orgId = run.organizationId
        lintoStore.userId = user?.id ? String(user.id) : undefined
        lintoStore.startedByMe = true
        lintoStore.error = undefined
      },
      onError: (err) => {
        const { code, message } = parseApiError(err)
        lintoStore.error =
          code === 'permission_denied'
            ? t('errors.generic')
            : (message ?? t('errors.generic'))
      },
    })
  const { mutateAsync: stopLintoLive, isPending: isPendingToStop } =
    useStopLintoLive({
      onSuccess: () => {
        resetLintoRun()
      },
    })

  const handleStart = async () => {
    if (!roomId || !token || !roomSlug || !lintoConfig) {
      console.warn('LinTO: missing room id, token, slug or config')
      return
    }
    // Debounce an accidental start right after a local action (a stray click can
    // land on the Start button as it replaces the Stop button when a run ends —
    // it must not immediately relaunch the bot).
    if (lintoStore.running || Date.now() < lintoStore.localActionUntil) return
    // Clear any previous error / transcript before starting a new run.
    lintoStore.error = undefined
    clearTranscript()
    lintoStore.localActionUntil = Date.now() + LOCAL_ACTION_GUARD_MS
    try {
      await startLintoLive({
        roomId,
        token,
        roomSlug,
        lintoConfig,
        config: {
          summary,
          record: hasScreenRecordingAccess ? record : false,
          translations: [...lintoStore.selectedTranslations],
        },
      })
    } catch (err) {
      // The error message is surfaced via the store (onError); keep a console
      // trace for debugging.
      console.error('Failed to start LinTO transcription:', err)
    }
  }

  const handleStop = async () => {
    if (!roomId || !token) {
      console.warn('LinTO: missing room id or token')
      return
    }
    // Snapshot the summary intent BEFORE stopping — the toast only fires when a
    // summary email is actually on its way.
    const summaryWasOn = lintoStore.summary
    const startedByMe = lintoStore.startedByMe
    lintoStore.localActionUntil = Date.now() + LOCAL_ACTION_GUARD_MS
    try {
      await stopLintoLive({
        roomId,
        token,
        run: {
          sessionId: lintoStore.sessionId,
          channelId: lintoStore.channelId,
          botId: lintoStore.botId ?? null,
          organizationId: lintoStore.orgId,
        },
      })
      if (summaryWasOn) {
        notifyLintoSummarySaving(startedByMe, user?.email)
      }
    } catch (err) {
      console.error('Failed to stop LinTO transcription:', err)
    }
  }

  const handleRequestTranscription = async () => {
    await notifyParticipants({ type: NotificationType.TranscriptionRequested })
  }

  if (!enabled) {
    return (
      <Div
        data-testid="linto-panel"
        display="flex"
        padding="0 1.5rem"
        flexGrow={1}
        flexDirection="column"
        alignItems="center"
        justifyContent="center"
      >
        <Text variant="note" centered>
          {t('notAvailable')}
        </Text>
      </Div>
    )
  }

  // No transcription rights AND no bot running yet → full NoAccess takeover
  // (calque TranscriptSidePanel). When a bot is already running, a viewer
  // without rights still sees the live transcript below (read-only).
  if (!running && hasTranscriptNoAccess) {
    return (
      <div
        data-testid="linto-no-access"
        className={css({
          display: 'flex',
          flexGrow: 1,
          width: '100%',
          flexDirection: 'column',
        })}
      >
        <NoAccessView
          i18nKeyPrefix="transcript"
          i18nKey="notAdminOrOwner"
          helpArticle={configData?.support?.help_article_transcript}
          imagePath="/assets/intro-slider/3.png"
          handleRequest={handleRequestTranscription}
          isActive={running}
        />
      </div>
    )
  }

  // Has the right to start/configure the bot (false → read-only viewer).
  const canControl = !hasTranscriptNoAccess
  // Only the starter or a room admin/owner may stop a shared run.
  const canStop = running && (lintoStore.startedByMe || !!isAdminOrOwner)
  const controlsDisabled = isPendingToStart

  return (
    <Div
      data-testid="linto-panel"
      display="flex"
      overflowY="scroll"
      padding="0 1.5rem"
      flexGrow={1}
      flexDirection="column"
      alignItems="center"
    >
      <VStack gap={0} marginBottom={15}>
        <H lvl={1} margin={'sm'} fullWidth>
          {t('heading')}
        </H>
        <Text variant="body" fullWidth>
          {t('body')}
        </Text>
      </VStack>

      {canControl && !running && (
        <>
          <LintoSettings isDisabled={controlsDisabled} />

          <VStack
            gap={0.5}
            width="100%"
            marginBottom={20}
            alignItems="start"
            className={css({ width: '100%' })}
          >
            <Checkbox
              size="sm"
              data-testid="linto-mode-summary"
              isSelected={summary}
              onChange={(value) => {
                lintoStore.summary = value
              }}
              isDisabled={controlsDisabled}
            >
              <Text variant="sm">{t('options.summary')}</Text>
            </Checkbox>
            <Text variant="xsNote" className={css({ paddingLeft: '1.625rem' })}>
              {t('options.summaryHelp')}
            </Text>
            {hasScreenRecordingAccess && (
              <Checkbox
                size="sm"
                data-testid="linto-mode-record"
                isSelected={record}
                onChange={(value) => {
                  lintoStore.record = value
                }}
                isDisabled={controlsDisabled}
              >
                <Text variant="sm">{t('options.record')}</Text>
              </Checkbox>
            )}
          </VStack>
        </>
      )}

      {error && (
        <div
          data-testid="linto-error"
          role="alert"
          className={css({
            width: '100%',
            marginBottom: '0.75rem',
            padding: '0.625rem 0.75rem',
            borderRadius: '4px',
            border: '1px solid',
            borderColor: 'danger',
            backgroundColor: 'danger.subtle',
          })}
        >
          <HStack gap={0.5} alignItems="start">
            <Text variant="sm" aria-hidden="true">
              ⚠
            </Text>
            <VStack gap={0.375} alignItems="start" width="100%">
              <Text variant="warning" className={css({ textStyle: 'sm' })}>
                {error}
              </Text>
              <Button
                variant="text"
                size="sm"
                data-testid="linto-error-retry"
                onPress={handleStart}
                isDisabled={isPendingToStart || !roomId || noProfiles}
              >
                {t('errors.retry')}
              </Button>
            </VStack>
          </HStack>
        </div>
      )}

      <div className={css({ width: '100%', marginBottom: '1.5rem' })}>
        {running
          ? canStop && (
              <Button
                variant="tertiary"
                fullWidth
                data-testid="linto-stop"
                onPress={handleStop}
                isDisabled={isPendingToStop}
              >
                {t('stop')}
              </Button>
            )
          : canControl && (
              <Button
                variant="primary"
                fullWidth
                data-testid="linto-start"
                onPress={handleStart}
                isDisabled={isPendingToStart || !roomId || noProfiles}
              >
                {t('start')}
              </Button>
            )}
      </div>

      {running && <LiveTranscript />}
    </Div>
  )
}
