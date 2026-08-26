import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSubtitles } from '../hooks/useSubtitles'
import { css, cva } from '@/styled-system/css'
import { styled } from '@/styled-system/jsx'
import { Avatar } from '@/components/Avatar'
import { Text } from '@/primitives'
import { useRoomContext } from '@livekit/components-react'
import { getParticipantColor } from '@/features/rooms/utils/getParticipantColor'
import { getParticipantName } from '@/features/rooms/utils/getParticipantName'
import { type Participant, RoomEvent } from 'livekit-client'
import { useSnapshot } from 'valtio'
import {
  accessibilityStore,
  CAPTION_TEXT_SIZE_OPTIONS,
  CAPTION_FONT_COLOR_VALUES,
  CAPTION_BACKGROUND_COLOR_VALUES,
  type CaptionTextSize,
} from '@/stores/accessibility'
import { parseLintoSegmentId } from '@/features/transcription-bot/store/transcriptStore'
import { UNATTRIBUTED_SPEAKER } from '@/features/transcription-bot/hooks/useLintoTranscriptFeed'

const FONT_SIZE_CONFIG: Record<
  CaptionTextSize,
  { fontSize: string; lineHeight: string }
> = {
  small: { fontSize: '0.875rem', lineHeight: '1.2rem' },
  medium: { fontSize: '1.5rem', lineHeight: '1.7rem' },
  large: { fontSize: '2.25rem', lineHeight: '2.5rem' },
}

const CAPTION_FONT_SIZES = Object.fromEntries(
  CAPTION_TEXT_SIZE_OPTIONS.map((size) => [size, FONT_SIZE_CONFIG[size]])
) as Record<CaptionTextSize, { fontSize: string; lineHeight: string }>

// Keep the overlay bounded: only the tail of a long meeting matters here (the
// LinTO side panel keeps the full transcript).
const MAX_SEGMENTS = 200

// Fallback colour for lines that cannot be attributed to a participant (e.g.
// LinTO lines published under the hidden bot's identity).
const UNATTRIBUTED_COLOR = 'rgb(87, 44, 216)'

export interface TranscriptionSegment {
  id: string
  text: string
  language: string
  startTime?: number
  endTime: number
  final: boolean
  firstReceivedTime: number
  lastReceivedTime: number
}

/**
 * Who said it. Native LiveKit segments resolve to a room participant; LinTO
 * segments the bot could not attribute (or from a participant who already
 * left) fall back to a synthetic speaker so the line is still shown.
 */
export interface TranscriptionSpeaker {
  identity: string
  name: string
  color: string
}

export interface TranscriptionSegmentWithSpeaker extends TranscriptionSegment {
  speaker: TranscriptionSpeaker
}

export interface TranscriptionRow {
  id: string
  speaker: TranscriptionSpeaker
  segments: TranscriptionSegment[]
  startTime?: number
  lastUpdateTime: number
  lastReceivedTime: number
}

const speakerOf = (
  participant: Participant | undefined,
  segmentId: string
): TranscriptionSpeaker => {
  const isBot = participant?.identity.startsWith('linto-visio-bot-')
  if (participant && !isBot) {
    return {
      identity: participant.identity,
      name: getParticipantName(participant),
      color: getParticipantColor(participant),
    }
  }
  return {
    identity: `unattributed:${parseLintoSegmentId(segmentId) ? 'linto' : 'native'}`,
    name: UNATTRIBUTED_SPEAKER,
    color: UNATTRIBUTED_COLOR,
  }
}

const useTranscriptionState = () => {
  const [transcriptionSegments, setTranscriptionSegments] = useState<
    TranscriptionSegmentWithSpeaker[]
  >([])

  const updateTranscriptionSegments = useCallback(
    (segments: TranscriptionSegment[], participant?: Participant) => {
      if (segments.length === 0) return

      setTranscriptionSegments((prevSegments) => {
        let next = prevSegments
        for (const segment of segments) {
          // LinTO translations ride on the same event with a language-suffixed
          // id; the overlay only shows the spoken language.
          if (parseLintoSegmentId(segment.id)?.lang) continue
          const speaker = speakerOf(participant, segment.id)
          const index = next.findIndex((s) => s.id === segment.id)
          if (index === -1) {
            next = [...next, { ...segment, speaker }]
          } else {
            // Partial → final (or a longer partial) of the SAME utterance:
            // replace in place instead of ignoring it.
            const existing = next[index]
            if (existing.final && !segment.final) continue
            next = next.slice()
            next[index] = { ...existing, ...segment, speaker: existing.speaker }
          }
        }
        if (next.length > MAX_SEGMENTS) next = next.slice(-MAX_SEGMENTS)
        return next
      })
    },
    []
  )

  const clearTranscriptionSegments = useCallback(
    () => setTranscriptionSegments([]),
    []
  )

  return {
    updateTranscriptionSegments,
    clearTranscriptionSegments,
    transcriptionSegments,
  }
}

const Transcription = ({ row }: { row: TranscriptionRow }) => {
  const { captionTextSize, captionFontColor, captionBackgroundColor } =
    useSnapshot(accessibilityStore)
  const { fontSize, lineHeight } = CAPTION_FONT_SIZES[captionTextSize]
  const fontColor = CAPTION_FONT_COLOR_VALUES[captionFontColor]
  const backgroundColor =
    CAPTION_BACKGROUND_COLOR_VALUES[captionBackgroundColor]

  const getDisplayText = (row: TranscriptionRow): string => {
    return row.segments
      .filter((segment) => segment.text.trim())
      .map((segment) => segment.text.trim())
      .join(' ')
  }

  const displayText = getDisplayText(row)

  if (!displayText) return null

  return (
    <div
      className={css({
        maxWidth: '800px',
        width: '100%',
      })}
      data-testid="caption-overlay-line"
    >
      <div
        className={css({
          display: 'flex',
          gap: '0.5rem',
        })}
      >
        <Avatar
          name={row.speaker.name}
          bgColor={row.speaker.color}
          context="subtitles"
        />
        <div
          className={css({
            width: '100%',
          })}
          style={{ color: fontColor }}
        >
          <Text variant="h3" margin={false}>
            {row.speaker.name}
          </Text>
          <p
            className={css({
              fontWeight: '400',
              borderRadius: '4px',
              padding: '0.125rem 0.25rem',
            })}
            style={{ fontSize, lineHeight, backgroundColor }}
          >
            {displayText}
          </p>
        </div>
      </div>
    </div>
  )
}

const SubtitlesWrapper = styled(
  'div',
  cva({
    base: {
      width: '100%',
      paddingTop: 'var(--lk-grid-gap)',
      transition: 'height .5s cubic-bezier(0.4,0,0.2,1) 5ms',
    },
    variants: {
      areOpen: {
        true: {
          height: '12rem',
        },
        false: {
          height: '0',
        },
      },
    },
  })
)

export const Subtitles = () => {
  const { areSubtitlesOpen } = useSubtitles()
  const room = useRoomContext()

  const {
    transcriptionSegments,
    updateTranscriptionSegments,
    clearTranscriptionSegments,
  } = useTranscriptionState()

  useEffect(() => {
    if (!room) return
    room.on(RoomEvent.TranscriptionReceived, updateTranscriptionSegments)
    room.on(RoomEvent.Disconnected, clearTranscriptionSegments)
    return () => {
      room.off(RoomEvent.TranscriptionReceived, updateTranscriptionSegments)
      room.off(RoomEvent.Disconnected, clearTranscriptionSegments)
    }
  }, [room, updateTranscriptionSegments, clearTranscriptionSegments])

  const transcriptionRows = useMemo(() => {
    if (transcriptionSegments.length === 0) return []

    const rows: TranscriptionRow[] = []
    let currentRow: TranscriptionRow | null = null

    for (const segment of transcriptionSegments) {
      const shouldStartNewRow =
        !currentRow || currentRow.speaker.identity !== segment.speaker.identity

      if (shouldStartNewRow) {
        currentRow = {
          id: `${segment.speaker.identity}-${segment.firstReceivedTime}`,
          speaker: segment.speaker,
          segments: [segment],
          startTime: segment.startTime,
          lastUpdateTime: segment.lastReceivedTime,
          lastReceivedTime: segment.lastReceivedTime,
        }
        rows.push(currentRow)
      } else if (currentRow) {
        currentRow.segments.push(segment)
        currentRow.lastUpdateTime = Math.max(
          currentRow.lastUpdateTime,
          segment.lastReceivedTime
        )
        currentRow.lastReceivedTime = Math.max(
          currentRow.lastReceivedTime,
          segment.lastReceivedTime
        )
      }
    }
    return rows
  }, [transcriptionSegments])

  return (
    <SubtitlesWrapper areOpen={areSubtitlesOpen}>
      <div
        className={css({
          height: '100%',
          width: '100%',
          display: 'flex',
          gap: '1.25rem',
          flexDirection: 'column-reverse',
          overflowAnchor: 'auto',
          overflowY: 'scroll',
          padding: '0 1rem',
          alignItems: 'center',
        })}
      >
        {transcriptionRows
          .slice()
          .reverse()
          .map((row) => (
            <Transcription key={row.id} row={row} />
          ))}
      </div>
    </SubtitlesWrapper>
  )
}
