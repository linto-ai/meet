import { A, Div, Icon, Text } from '@/primitives'
import { css } from '@/styled-system/css'
import { Button as RACButton } from 'react-aria-components'
import { useTranslation } from 'react-i18next'
import { ReactNode, useEffect, useRef } from 'react'
import { SubPanelId, useSidePanel } from '../hooks/useSidePanel'
import { useRestoreFocus } from '@/hooks/useRestoreFocus'
import {
  useIsRecordingModeEnabled,
  RecordingMode,
  TranscriptSidePanel,
  ScreenRecordingSidePanel,
} from '@/features/recording'
import { useConfig } from '@/api/useConfig'
import { useRoomMetadata } from '@/features/recording/hooks/useRoomMetadata'
import {
  LintoSidePanel,
  useLintoConfig,
  useLintoEntitlement,
  useLintoStatus,
} from '@/features/transcription-bot'

export interface ToolsButtonProps {
  icon: ReactNode
  title: string
  description: string
  onPress: () => void
  dataAttr?: string
}

const ToolButton = ({
  icon,
  title,
  description,
  onPress,
  dataAttr,
}: ToolsButtonProps) => {
  return (
    <RACButton
      data-attr={dataAttr}
      className={css({
        display: 'flex',
        flexDirection: 'row',
        alignItems: 'center',
        justifyContent: 'start',
        paddingY: '0.5rem',
        paddingX: '0.75rem 1.5rem',
        borderRadius: '30px',
        width: 'full',
        backgroundColor: 'gray.50',
        textAlign: 'start',
        '&[data-hovered]': {
          backgroundColor: 'primary.50',
          cursor: 'pointer',
        },
      })}
      onPress={onPress}
    >
      <div
        className={css({
          height: '40px',
          minWidth: '40px',
          borderRadius: '25px',
          marginRight: '0.75rem',
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          position: 'relative',
          background: 'primary.800',
          color: 'white',
        })}
      >
        {icon}
      </div>
      <div>
        <Text
          margin={false}
          as="h2"
          className={css({
            display: 'flex',
            gap: 0.25,
            fontWeight: 'semibold',
          })}
        >
          {title}
        </Text>
        <Text as="p" variant="smNote" wrap="pretty">
          {description}
        </Text>
      </div>
      <div
        className={css({
          marginLeft: 'auto',
          height: '100%',
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
        })}
      >
        <Icon name="chevron_forward" />
      </div>
    </RACButton>
  )
}

export const Tools = () => {
  const { data } = useConfig()
  const {
    openTranscript,
    openScreenRecording,
    openLinto,
    activeSubPanelId,
    isToolsOpen,
    isSidePanelOpen,
  } = useSidePanel()
  const { t } = useTranslation('rooms', { keyPrefix: 'moreTools' })
  const { t: tLinto } = useTranslation('transcription-bot', {
    keyPrefix: 'tools',
  })
  const { enabled: isLintoEnabled } = useLintoConfig()
  // The LinTO entry is offered to participants whose account has the option
  // (a linked LinTO key); it stays visible for everyone while a transcription
  // runs, so the others can read it.
  const lintoEntitlement = useLintoEntitlement()
  const { active: isLintoActive } = useLintoStatus()
  const showLintoTool =
    isLintoEnabled && (isLintoActive || lintoEntitlement !== 'no_entitlement')

  // The three tools are exclusive: while one runs, opening the tools lands
  // straight on ITS panel (the list is one "back" away, the other two tools
  // keep their "another mode is running" notice). Decided when the panel
  // OPENS only — pressing back must show the list, not bounce.
  const metadata = useRoomMetadata()
  const recordingMode = metadata?.recording_mode as string | undefined
  const recordingActive =
    !!recordingMode &&
    ['starting', 'started', 'saving'].includes(
      String(metadata?.recording_status ?? '')
    )
  const wasToolsOpenRef = useRef(isToolsOpen)
  useEffect(() => {
    const justOpened = isToolsOpen && !wasToolsOpenRef.current
    wasToolsOpenRef.current = isToolsOpen
    if (!justOpened || activeSubPanelId) return
    if (isLintoActive && isLintoEnabled) openLinto()
    else if (recordingActive && recordingMode === 'transcript') openTranscript()
    else if (recordingActive) openScreenRecording()
    // Only the opening transition matters; the run state is read at that moment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isToolsOpen])

  // Restore focus to the element that opened the Tools panel
  // following the same pattern as Chat.
  useRestoreFocus(isToolsOpen, {
    // If the active element is a MenuItem (DIV) that will be unmounted when the menu closes,
    // find the "more options" button ("Plus d'options") that opened the menu
    resolveTrigger: (activeEl) => {
      if (activeEl?.tagName === 'DIV') {
        return document.querySelector<HTMLElement>('#room-options-trigger')
      }
      // For direct button clicks (e.g. "Plus d'outils"), use the active element as is
      return activeEl
    },
    restoreFocusRaf: true,
    preventScroll: true,
    shouldRestoreOnClose: () => !isSidePanelOpen,
  })

  const isTranscriptEnabled = useIsRecordingModeEnabled(
    RecordingMode.Transcript
  )

  const isScreenRecordingEnabled = useIsRecordingModeEnabled(
    RecordingMode.ScreenRecording
  )

  switch (activeSubPanelId) {
    case SubPanelId.TRANSCRIPT:
      return <TranscriptSidePanel />
    case SubPanelId.SCREEN_RECORDING:
      return <ScreenRecordingSidePanel />
    case SubPanelId.LINTO:
      return <LintoSidePanel />
    default:
      break
  }

  return (
    <Div
      display="flex"
      overflowY="scroll"
      padding="0 0.75rem"
      flexGrow={1}
      flexDirection="column"
      alignItems="start"
      gap={0.5}
    >
      <Text
        variant="note"
        wrap="balance"
        className={css({
          textStyle: 'sm',
          paddingX: '0.75rem',
          paddingTop: '0.25rem',
          marginBottom: '1rem',
        })}
      >
        {t('body')}{' '}
        {data?.support?.help_article_more_tools && (
          <A
            href={data.support.help_article_more_tools}
            target="_blank"
            rel="noopener noreferrer"
            externalIcon
            color="note"
            aria-label={t('linkAriaLabel')}
          >
            {t('moreLink')}
          </A>
        )}
      </Text>
      {/* Two distinct transcription tools: the live one (LinTO bot in the
          room: captions, translation, summaries) and the deferred one (the
          meeting is recorded, transcribed once it ends). */}
      {showLintoTool && (
        <ToolButton
          icon={<Icon name="speech_to_text" />}
          title={tLinto('live.title')}
          description={tLinto('live.body')}
          onPress={() => openLinto()}
          dataAttr="tool-linto-transcription"
        />
      )}
      {isTranscriptEnabled && (
        <ToolButton
          icon={<Icon name="speech_to_text" />}
          title={
            isLintoEnabled
              ? tLinto('deferred.title')
              : t('tools.transcript.title')
          }
          description={
            isLintoEnabled
              ? tLinto('deferred.body')
              : t('tools.transcript.body')
          }
          onPress={() => openTranscript()}
          dataAttr="tool-transcript"
        />
      )}
      {isScreenRecordingEnabled && (
        <ToolButton
          icon={<Icon name="mode_standby" />}
          title={t('tools.screenRecording.title')}
          description={t('tools.screenRecording.body')}
          onPress={() => openScreenRecording()}
        />
      )}
    </Div>
  )
}
