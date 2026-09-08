import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import { lintoStore } from '@/features/transcription-bot/store/lintoStore'

const ORIGINAL = 'original'
// Collapse region-tagged codes ("en-US") to their base ("en") for matching.
const baseCode = (code: string): string => code.split('-')[0].toLowerCase()
// Pick a segment's text in the chosen display language (translation when present
// for that base code), else the original.
const textFor = (
  segment: { text: string; translations?: Record<string, string> },
  displayLanguage: string
): string => {
  if (displayLanguage === ORIGINAL || !segment.translations) return segment.text
  const direct = segment.translations[displayLanguage]
  if (direct != null) return direct
  const hit = Object.entries(segment.translations).find(
    ([k]) => baseCode(k) === displayLanguage
  )
  return hit?.[1] ?? segment.text
}

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
  // LinTO live translations keyed by target language code (from the
  // `linto:<seg>:<lang>` segments the bot publishes alongside the original).
  translations?: Record<string, string>
}

export interface TranscriptionRow {
  id: string
  speaker: TranscriptionSpeaker
  // One row = one caption/utterance (finals are never concatenated). The speaker
  // header is shown only when it changes from the previous row.
  segment: TranscriptionSegmentWithSpeaker
  showSpeaker: boolean
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

  // Translations that arrived before their original segment: baseId → {lang:text}.
  const pendingTranslations = useRef<Record<string, Record<string, string>>>({})

  const updateTranscriptionSegments = useCallback(
    (segments: TranscriptionSegment[], participant?: Participant) => {
      if (segments.length === 0) return

      setTranscriptionSegments((prevSegments) => {
        let next = prevSegments
        for (const segment of segments) {
          const parsed = parseLintoSegmentId(segment.id)
          // A LinTO translation segment (`linto:[<channel>:]<seg>:<lang>`): merge its text onto
          // the original segment (`linto:[<channel>:]<seg>`) under the target language; buffer
          // it when the original hasn't arrived yet.
          if (parsed?.lang) {
            const baseId = parsed.base
            const idx = next.findIndex((s) => s.id === baseId)
            if (idx === -1) {
              const buf = pendingTranslations.current[baseId] ?? {}
              buf[parsed.lang] = segment.text
              pendingTranslations.current[baseId] = buf
            } else {
              next = next.slice()
              next[idx] = {
                ...next[idx],
                translations: {
                  ...next[idx].translations,
                  [parsed.lang]: segment.text,
                },
              }
            }
            continue
          }
          const speaker = speakerOf(participant, segment.id)
          const index = next.findIndex((s) => s.id === segment.id)
          const pending = pendingTranslations.current[segment.id]
          if (index === -1) {
            next = [
              ...next,
              {
                ...segment,
                speaker,
                ...(pending && { translations: pending }),
              },
            ]
            if (pending) delete pendingTranslations.current[segment.id]
          } else {
            // Partial → final (or a longer partial) of the SAME utterance:
            // replace in place instead of ignoring it (keep translations).
            const existing = next[index]
            if (existing.final && !segment.final) continue
            next = next.slice()
            next[index] = {
              ...existing,
              ...segment,
              speaker: existing.speaker,
              translations: { ...existing.translations, ...pending },
            }
            if (pending) delete pendingTranslations.current[segment.id]
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

const Transcription = ({
  row,
  displayLanguage,
}: {
  row: TranscriptionRow
  displayLanguage: string
}) => {
  const { captionTextSize, captionFontColor, captionBackgroundColor } =
    useSnapshot(accessibilityStore)
  const { fontSize, lineHeight } = CAPTION_FONT_SIZES[captionTextSize]
  const fontColor = CAPTION_FONT_COLOR_VALUES[captionFontColor]
  const backgroundColor =
    CAPTION_BACKGROUND_COLOR_VALUES[captionBackgroundColor]

  // One row = one utterance (never concatenated). The avatar + name are shown
  // only when the speaker changes; a continuation line aligns under the name.
  const displayText = textFor(row.segment, displayLanguage).trim()

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
        {row.showSpeaker ? (
          <Avatar
            name={row.speaker.name}
            bgColor={row.speaker.color}
            context="subtitles"
          />
        ) : (
          // Keep the text aligned under the name when the header is hidden.
          <div
            className={css({ flexShrink: 0, width: '2.5rem' })}
            aria-hidden
          />
        )}
        <div
          className={css({
            width: '100%',
          })}
          style={{ color: fontColor }}
        >
          {row.showSpeaker && (
            <Text variant="h3" margin={false}>
              {row.speaker.name}
            </Text>
          )}
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
  // Shared with the LinTO panel: switching the "displayed language" there also
  // switches the overlay (translation shown when available, else the original).
  const { displayLanguage } = useSnapshot(lintoStore)

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

  const transcriptionRows = useMemo(
    () =>
      transcriptionSegments.map((segment, index) => ({
        id: segment.id,
        speaker: segment.speaker,
        segment,
        showSpeaker:
          index === 0 ||
          transcriptionSegments[index - 1].speaker.identity !==
            segment.speaker.identity,
      })),
    [transcriptionSegments]
  )

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
            <Transcription
              key={row.id}
              row={row}
              displayLanguage={displayLanguage}
            />
          ))}
      </div>
    </SubtitlesWrapper>
  )
}
