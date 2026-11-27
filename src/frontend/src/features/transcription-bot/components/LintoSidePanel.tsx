import { useEffect } from 'react'
import { Button, Div, H, Text } from '@/primitives'
import { css } from '@/styled-system/css'
import { HStack, VStack } from '@/styled-system/jsx'
import { Checkbox } from '@/primitives/Checkbox'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { useConfig } from '@/api/useConfig'
import { useUser } from '@/features/auth/api/useUser'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useIsAdminOrOwner } from '@/features/rooms/livekit/hooks/useIsAdminOrOwner'
import {
  RecordingMode,
  useHasRecordingAccess,
  useHasFeatureWithoutRecordingRights,
} from '@/features/recording'
import { useRoomRecording } from '@/features/recording/hooks/useRoomRecording'
import { useSidePanel } from '@/features/rooms/livekit/hooks/useSidePanel'
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
import { useLintoCapabilities } from '../hooks/useLintoCapabilities'
import { useLintoCaptionsOnly } from '../hooks/useLintoCaptionsOnly'
import {
  useLintoBotProfiles,
  useStartLintoLive,
  useStopLintoLive,
} from '../api/lintoBotApi'
import { lintoStartFailure } from '../utils/startFailure'
import { TranslationPicker } from './TranslationPicker'
import { SummaryServicePicker } from './SummaryServicePicker'
import { LiveTranscript } from './LiveTranscript'
import { PanelLinkRow, RecordingDot } from './PanelLinkRow'

// Suppress the room-wide state sync from clobbering optimistic state for a
// short window after a local start/stop (the metadata roundtrip lags the
// mutation result).
const LOCAL_ACTION_GUARD_MS = 6000

export const LintoSidePanel = () => {
  const { t } = useTranslation('transcription-bot', { keyPrefix: 'lintoBot' })
  const { t: tRecording } = useTranslation('rooms', {
    keyPrefix: 'recordingStateToast',
  })

  const { enabled } = useLintoConfig()
  const { running, summary, summaryService, shareSummary, record, error } =
    useSnapshot(lintoStore)

  const apiRoomData = useRoomData()
  const roomId = apiRoomData?.livekit?.room
  const token = apiRoomData?.livekit?.token

  const { user } = useUser()
  const {
    loading: capabilitiesLoading,
    live: canTranscribeLive,
    summary: canSummarize,
  } = useLintoCapabilities()
  const { data: configData } = useConfig()
  const lintoConfig = configData?.linto
  const isAdminOrOwner = useIsAdminOrOwner()
  const { notifyParticipants } = useNotifyParticipants()
  // While the meeting is recorded (deferred transcription or video), the
  // recording is its memory: the live tool only shows captions — translations
  // to pick, nothing kept. A running run keeps the role it started with.
  const { isRecorded, mode: recordingMode } = useRoomRecording()
  const { openTranscript, openScreenRecording } = useSidePanel()
  const openRecordingTool =
    recordingMode === 'transcript' ? openTranscript : openScreenRecording
  const captionsOnly = useLintoCaptionsOnly()

  // Permission gating (mirrors the legacy recording machinery).
  const hasTranscriptNoAccess = useHasFeatureWithoutRecordingRights(
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
        lintoStore.captionsOnly = run.captions_only === true
        lintoStore.userId = user?.id ? String(user.id) : undefined
        lintoStore.startedByMe = true
        lintoStore.error = undefined
      },
      onError: (err) => {
        const failure = lintoStartFailure(err)
        if (failure.kind === 'error') {
          lintoStore.error = t(failure.messageKey, failure.params)
          return
        }
        // The room already has a run: its metadata takes over.
        lintoStore.localActionUntil = 0
        if (failure.kind === 'already_running') lintoStore.running = true
      },
    })
  const { mutateAsync: stopLintoLive, isPending: isPendingToStop } =
    useStopLintoLive({
      onSuccess: () => {
        resetLintoRun()
      },
    })

  const handleStart = async () => {
    if (!roomId || !token) {
      console.warn('LinTO: missing room id or token')
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
      const translations = [...lintoStore.selectedTranslations]
      await startLintoLive({
        roomId,
        token,
        config: captionsOnly
          ? {
              summary: false,
              shareSummary: false,
              catchup: false,
              record: false,
              translations,
              captionsOnly: true,
            }
          : {
              // The summaries are a feature of their own: without the
              // capability both options are hidden and the run carries none.
              summary: canSummarize && summary,
              summaryService,
              shareSummary: canSummarize && summary && shareSummary,
              catchup: canSummarize,
              record: hasScreenRecordingAccess ? record : false,
              translations,
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
    lintoStore.localActionUntil = Date.now() + LOCAL_ACTION_GUARD_MS
    try {
      // The recap email always goes out: the answer says to whom, and whether
      // it holds a summary (shared with the participants) or the transcript.
      // Captions only: nothing goes out, the panel itself shows the stop.
      const result = await stopLintoLive({ roomId, token })
      if (result.status === 'stopped' && result.captions_only !== true) {
        notifyLintoSummarySaving({
          recipientIsMe: result.recipient_is_you === true,
          email: user?.email,
          summary: result.summary !== false,
          shared: result.share_summary === true,
          noSpeech: result.no_speech === true,
        })
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

  // The LinTO option is not active for this account: the capabilities of
  // users/me do not grant the live transcription (decided before any Studio
  // call), or the identity bridge itself answered "not entitled". Nothing to
  // start; a run started by someone else is still readable below, exactly
  // like a viewer without rights.
  const noEntitlement =
    (!capabilitiesLoading && !canTranscribeLive) ||
    profilesData?.reason === 'no_entitlement'
  if (!running && noEntitlement) {
    return (
      <Div
        data-testid="linto-no-entitlement"
        display="flex"
        padding="0 1.5rem"
        flexGrow={1}
        flexDirection="column"
        alignItems="center"
        justifyContent="center"
      >
        <Text variant="note" centered>
          {t('noEntitlement')}
        </Text>
      </Div>
    )
  }

  // Has the right to start/configure the bot (false → read-only viewer).
  const canControl = !hasTranscriptNoAccess && !noEntitlement
  // Only the starter or a room admin/owner may stop a shared run.
  const canStop = running && (lintoStore.startedByMe || !!isAdminOrOwner)
  const controlsDisabled = isPendingToStart

  return (
    <Div
      data-testid="linto-panel"
      display="flex"
      // While a run shows, the transcript owns the scrollbar (its own zone
      // below the controls); the panel itself no longer scrolls as a whole.
      overflowY={running ? 'hidden' : 'auto'}
      padding="0 1.5rem"
      flexGrow={1}
      flexDirection="column"
      alignItems="center"
      className={css({ minHeight: 0 })}
    >
      <VStack gap={0} marginBottom={15} className={css({ flexShrink: 0 })}>
        <H lvl={1} margin={'sm'} fullWidth>
          {captionsOnly ? t('captionsOnly.heading') : t('heading')}
        </H>
        {!running && !captionsOnly && (
          <Text variant="body" fullWidth>
            {t('body')}
          </Text>
        )}
      </VStack>

      {/* Beside a recording, the recording says why nothing else is offered
          and leads to its own panel. */}
      {captionsOnly && isRecorded && (
        <div
          className={css({
            width: '100%',
            marginBottom: '1rem',
            flexShrink: 0,
          })}
        >
          <PanelLinkRow
            testId="linto-recording-row"
            icon={<RecordingDot />}
            label={tRecording('any.started')}
            onPress={openRecordingTool}
          />
        </div>
      )}

      {canControl && !running && captionsOnly && (
        <VStack
          gap={0.75}
          width="100%"
          marginBottom={20}
          alignItems="start"
          className={css({ width: '100%' })}
        >
          <TranslationPicker isDisabled={controlsDisabled} />
        </VStack>
      )}

      {canControl && !running && !captionsOnly && (
        <>
          {/* Flat options, as the Studio mobile app lays them out: the
              translation targets first, then the summaries and the video. */}
          <VStack
            gap={0.75}
            width="100%"
            marginBottom={20}
            alignItems="start"
            className={css({ width: '100%' })}
          >
            <TranslationPicker isDisabled={controlsDisabled} />
            {canSummarize && (
              <>
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
                {summary && (
                  <>
                    <SummaryServicePicker isDisabled={controlsDisabled} />
                    <Checkbox
                      size="sm"
                      data-testid="linto-mode-share-summary"
                      isSelected={shareSummary}
                      onChange={(value) => {
                        lintoStore.shareSummary = value
                      }}
                      isDisabled={controlsDisabled}
                    >
                      <Text variant="sm">{t('options.shareSummary')}</Text>
                    </Checkbox>
                  </>
                )}
              </>
            )}
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

      <div
        className={css({
          width: '100%',
          marginBottom: running ? '0.75rem' : '1.5rem',
          flexShrink: 0,
        })}
      >
        {running
          ? canStop && (
              <Button
                variant="tertiary"
                fullWidth
                data-testid="linto-stop"
                onPress={handleStop}
                isDisabled={isPendingToStop}
              >
                {captionsOnly ? t('captionsOnly.stop') : t('stop')}
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
                {captionsOnly ? t('captionsOnly.start') : t('start')}
              </Button>
            )}
      </div>

      {running && <LiveTranscript />}
    </Div>
  )
}
