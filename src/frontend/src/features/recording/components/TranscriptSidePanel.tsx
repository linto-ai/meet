import { A, Button, Div, H, Text } from '@/primitives'

import { css } from '@/styled-system/css'
import { useRoomId } from '@/features/rooms/livekit/hooks/useRoomId'
import { useRoomContext } from '@livekit/components-react'
import {
  RecordingMode,
  useHasRecordingAccess,
  useHasFeatureWithoutAdminRights,
  useRecordingStatuses,
} from '../index'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { FeatureFlags } from '@/features/analytics/enums'
import {
  NotificationType,
  useNotifyParticipants,
  notifyRecordingSaveInProgress,
} from '@/features/notifications'
import { useConfig } from '@/api/useConfig'
import { VStack } from '@/styled-system/jsx'
import { Checkbox } from '@/primitives/Checkbox.tsx'

import {
  SettingsDialogExtendedKey,
  useTranscriptionLanguage,
} from '@/features/settings'
import { NoAccessView } from './NoAccessView'
import { ControlsButton } from './ControlsButton'
import { RowWrapper } from './RowWrapper'
import { useMutateRecording } from '../hooks/useMutateRecording'
import { useIsMetadataCollectorEnabled } from '../hooks/useMetadataCollectorEnabled'
import { useSidePanel } from '@/features/rooms/livekit/hooks/useSidePanel'
import { useIsAdminOrOwner } from '@/features/rooms/livekit/hooks/useIsAdminOrOwner'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { LimitDescription } from './LimitDescription'
import { openSettingsDialog } from '@/stores/settings'
import { LINTO_LANGUAGE_AUTO, recordingStore } from '@/stores/recording'
import { captureEvent, reportError } from '@/features/analytics/telemetry'
import {
  SummaryServicePicker,
  useLintoCapabilities,
  useLintoConfig,
  useLintoStatus,
  useLintoTranscriptionLanguages,
} from '@/features/transcription-bot'
import { languageName } from '@/features/transcription-bot/utils/languageLabel'

const selectClass = css({
  width: '100%',
  padding: '0.4rem',
  borderRadius: '4px',
  border: '1px solid',
  borderColor: 'control.border',
  backgroundColor: 'white',
})

/**
 * "Transcribe after the meeting": the meeting is recorded (audio, or video
 * when asked) and transcribed once it ends — the deferred counterpart of the
 * live LinTO tool. When LinTO serves the transcription, the panel offers the
 * languages its STT services know (automatic detection by default), the
 * summary and its service, and the video; otherwise the upstream form stays.
 */
export const TranscriptSidePanel = () => {
  const { data } = useConfig()

  const keyPrefix = 'transcript'
  const { t } = useTranslation('rooms', { keyPrefix })
  const { t: tLinto } = useTranslation('transcription-bot', {
    keyPrefix: 'deferred',
  })

  const [includeScreenRecording, setIncludeScreenRecording] = useState(false)

  const { notifyParticipants } = useNotifyParticipants()
  const { selectedLanguageKey, selectedLanguageLabel, isLanguageSetToAuto } =
    useTranscriptionLanguage()

  // LinTO (fork): the deferred transcription is a LinTO feature with its own
  // language list, summary options and per-user capability.
  const { enabled: isLintoEnabled } = useLintoConfig()
  const { loading: capabilitiesLoading, async: canTranscribeAsync } =
    useLintoCapabilities()
  const { active: isLintoActive } = useLintoStatus()
  const { openLinto } = useSidePanel()
  const apiRoomData = useRoomData()
  const { data: lintoLanguages } = useLintoTranscriptionLanguages(
    apiRoomData?.livekit?.room,
    apiRoomData?.livekit?.token
  )
  const { lintoLanguage, lintoSummary, lintoSummaryService } =
    useSnapshot(recordingStore)
  const { i18n } = useTranslation()
  // The ASR languages without the automatic one, named in the UI language.
  const languageOptions = (lintoLanguages ?? [])
    .filter((code) => code !== '*')
    .map((code) => ({ code, label: languageName(code, i18n.language) }))
    .sort((a, b) => a.label.localeCompare(b.label, i18n.language))

  const hasTranscriptAccess = useHasRecordingAccess(
    RecordingMode.Transcript,
    FeatureFlags.Transcript
  )

  const hasFeatureWithoutAdminRights = useHasFeatureWithoutAdminRights(
    RecordingMode.Transcript,
    FeatureFlags.Transcript
  )

  const isAdminOrOwner = useIsAdminOrOwner()

  const isMetadataCollectorEnabled = useIsMetadataCollectorEnabled()

  const roomId = useRoomId()

  const { startRecording, isPendingToStart, stopRecording, isPendingToStop } =
    useMutateRecording()

  const statuses = useRecordingStatuses(RecordingMode.Transcript)

  const room = useRoomContext()
  const { openScreenRecording } = useSidePanel()

  const handleRequestTranscription = async () => {
    await notifyParticipants({
      type: NotificationType.TranscriptionRequested,
    })
    captureEvent('transcript-requested', {})
  }

  const handleTranscript = async () => {
    if (!roomId) {
      console.warn('No room ID found')
      return
    }
    try {
      if (statuses.isStarted || statuses.isStarting) {
        await stopRecording({ id: roomId })
        setIncludeScreenRecording(false)

        await notifyParticipants({
          type: NotificationType.TranscriptionStopped,
        })
        notifyRecordingSaveInProgress(
          RecordingMode.Transcript,
          room.localParticipant
        )
      } else {
        const recordingMode = includeScreenRecording
          ? RecordingMode.ScreenRecording
          : RecordingMode.Transcript

        // LinTO: the language is an ASR code ('auto' = detected); the summary
        // choice rides along for the offline pipeline.
        const language = isLintoEnabled
          ? lintoLanguage !== LINTO_LANGUAGE_AUTO
            ? lintoLanguage
            : undefined
          : !isLanguageSetToAuto
            ? selectedLanguageKey
            : undefined
        const recordingOptions = {
          ...(language && { language }),
          ...(includeScreenRecording && {
            transcribe: true,
            original_mode: RecordingMode.Transcript,
          }),
          ...(isLintoEnabled && {
            summary: lintoSummary,
            ...(lintoSummary &&
              lintoSummaryService && {
                summary_service: lintoSummaryService,
              }),
          }),
          collect_metadata: isMetadataCollectorEnabled,
        }

        await startRecording({
          id: roomId,
          mode: recordingMode,
          options: recordingOptions,
        })

        await notifyParticipants({
          type: NotificationType.TranscriptionStarted,
        })
        captureEvent('transcript-started', {
          includeScreenRecording: includeScreenRecording,
          language: language ?? 'auto',
        })
      }
    } catch (error) {
      reportError('generic_failure', error, {
        context: 'Failed to handle transcript:',
      })
    }
  }

  if (hasFeatureWithoutAdminRights) {
    return (
      <NoAccessView
        i18nKeyPrefix={keyPrefix}
        i18nKey="notAdminOrOwner"
        helpArticle={data?.support?.help_article_transcript}
        imagePath="/assets/intro-slider/3.png"
        handleRequest={handleRequestTranscription}
        isActive={statuses.isActive}
      />
    )
  }

  if (!hasTranscriptAccess) {
    return (
      <NoAccessView
        i18nKeyPrefix={keyPrefix}
        i18nKey="premium"
        helpArticle={data?.support?.help_article_transcript}
        imagePath="/assets/intro-slider/3.png"
        handleRequest={handleRequestTranscription}
        isActive={statuses.isActive}
        isAdminOrOwner={isAdminOrOwner}
      />
    )
  }

  // The deferred transcription is not active for this account (LinTO
  // capability `transcription.async`): nothing to start. A recording someone
  // else runs still shows its state below.
  if (
    isLintoEnabled &&
    !capabilitiesLoading &&
    !canTranscribeAsync &&
    !statuses.isActive
  ) {
    return (
      <Div
        data-testid="transcript-no-entitlement"
        display="flex"
        padding="0 1.5rem"
        flexGrow={1}
        flexDirection="column"
        alignItems="center"
        justifyContent="center"
      >
        <Text variant="note" centered>
          {tLinto('noEntitlement')}
        </Text>
      </Div>
    )
  }

  const controlsDisabled = statuses.isActive || isPendingToStart

  return (
    <Div
      data-testid="transcript-panel"
      display="flex"
      overflowY="scroll"
      padding="0 1.5rem"
      flexGrow={1}
      flexDirection="column"
      alignItems="center"
    >
      {!isLintoEnabled && (
        <img
          src="/assets/intro-slider/3.png"
          alt=""
          className={css({
            minHeight: '250px',
            height: '250px',
            marginBottom: '1rem',
            marginTop: '-16px',
            '@media (max-height: 900px)': {
              height: 'auto',
              minHeight: 'auto',
              maxHeight: '25%',
              marginBottom: '0.75rem',
            },
            '@media (max-height: 770px)': {
              display: 'none',
            },
          })}
        />
      )}
      <VStack gap={0} marginBottom={15}>
        <H lvl={1} margin={'sm'} fullWidth>
          {isLintoEnabled ? tLinto('heading') : t('heading')}
        </H>
        {isLintoEnabled ? (
          <Text variant="body" fullWidth>
            {tLinto('body')}
          </Text>
        ) : (
          <LimitDescription
            keyPrefix={'transcript'}
            supportArticleLink={data?.support?.help_article_transcript}
          />
        )}
      </VStack>
      <VStack gap={0} marginBottom={25}>
        <RowWrapper iconName="article" position="first">
          <Text variant="sm">
            {data?.transcription_destination ? (
              <>
                {t('details.destination')}{' '}
                <A
                  href={data.transcription_destination}
                  target="_blank"
                  rel="noopener noreferrer"
                  externalIcon
                >
                  {data.transcription_destination.replace('https://', '')}
                </A>
              </>
            ) : (
              t('details.destinationUnknown')
            )}
          </Text>
        </RowWrapper>
        <RowWrapper
          iconName="mail"
          position={isLintoEnabled ? 'last' : 'middle'}
        >
          <Text variant="sm">{t('details.receiver')}</Text>
        </RowWrapper>
        {!isLintoEnabled && (
          <RowWrapper iconName="language" position="last">
            <Text variant="sm">{t('details.language')}</Text>
            <Text variant="sm">
              <Button
                variant="text"
                size="xs"
                onPress={() =>
                  openSettingsDialog(SettingsDialogExtendedKey.TRANSCRIPTION)
                }
              >
                {selectedLanguageLabel}
              </Button>
            </Text>
          </RowWrapper>
        )}
        <div className={css({ height: '15px' })} />
        {isLintoEnabled && (
          <VStack
            gap={0.75}
            width="100%"
            alignItems="start"
            className={css({ width: '100%', marginBottom: '0.75rem' })}
          >
            <label className={css({ width: '100%' })}>
              <Text variant="sm" as="span">
                {tLinto('language')}
              </Text>
              <select
                data-testid="transcript-language"
                className={selectClass}
                value={lintoLanguage}
                disabled={controlsDisabled}
                onChange={(e) => {
                  recordingStore.lintoLanguage = e.target.value
                }}
              >
                <option value={LINTO_LANGUAGE_AUTO}>
                  {tLinto('languageAuto')}
                </option>
                {languageOptions.map((option) => (
                  <option key={option.code} value={option.code}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <Checkbox
              size="sm"
              data-testid="transcript-mode-summary"
              isSelected={lintoSummary}
              onChange={(value) => {
                recordingStore.lintoSummary = value
              }}
              isDisabled={controlsDisabled}
            >
              <Text variant="sm">{tLinto('summary')}</Text>
            </Checkbox>
            {lintoSummary && (
              <SummaryServicePicker
                isDisabled={controlsDisabled}
                value={lintoSummaryService}
                onChange={(route) => {
                  recordingStore.lintoSummaryService = route
                }}
              />
            )}
          </VStack>
        )}
        <div
          className={css({
            width: '100%',
            marginLeft: isLintoEnabled ? 0 : '20px',
          })}
        >
          <Checkbox
            size="sm"
            data-testid="transcript-mode-record"
            isSelected={includeScreenRecording}
            onChange={setIncludeScreenRecording}
            isDisabled={controlsDisabled}
          >
            <Text variant="sm">
              {isLintoEnabled ? tLinto('record') : t('details.recording')}
            </Text>
          </Checkbox>
        </div>
      </VStack>
      <ControlsButton
        i18nKeyPrefix={keyPrefix}
        handle={handleTranscript}
        statuses={{
          ...statuses,
          // A live LinTO transcription owns the room too: exclusive.
          isAnotherModeStarted:
            statuses.isAnotherModeStarted ||
            (isLintoActive && !statuses.isActive),
        }}
        isPendingToStart={isPendingToStart}
        isPendingToStop={isPendingToStop}
        openSidePanel={
          isLintoActive && !statuses.isActive ? openLinto : openScreenRecording
        }
        anotherModeKey={
          isLintoActive && !statuses.isActive
            ? 'button.liveStarted'
            : 'button.anotherModeStarted'
        }
      />
    </Div>
  )
}
