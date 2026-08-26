import { useEffect } from 'react'
import { useRoomContext } from '@livekit/components-react'
import {
  type Participant,
  RoomEvent,
  type TranscriptionSegment,
} from 'livekit-client'
import { getParticipantName } from '@/features/rooms/utils/getParticipantName'
import {
  clearTranscript,
  parseLintoSegmentId,
  upsertCaption,
  upsertTranslation,
} from '../store/transcriptStore'
import { useLintoStatus } from './useLintoStatus'

// Display name used for lines the bot could not attribute to a participant
// (it then publishes them under its own — hidden — identity).
export const UNATTRIBUTED_SPEAKER = 'LinTO'

/**
 * Feed `transcriptStore` from the LiveKit transcription segments the LinTO bot
 * publishes into the room (native `RoomEvent.TranscriptionReceived`; ids are
 * namespaced `linto:<segmentId>[:<lang>]`). Mount ONCE per room (LintoProvider):
 * the panel and the caption overlay both read the store.
 *
 * The transcript is cleared when the room-wide transcription state turns off,
 * so a later run starts from a blank panel.
 */
export const useLintoTranscriptFeed = () => {
  const room = useRoomContext()
  const { active } = useLintoStatus()

  useEffect(() => {
    if (!room) return

    const onTranscription = (
      segments: TranscriptionSegment[],
      participant?: Participant
    ) => {
      const receivedAt = Date.now()
      for (const segment of segments) {
        const parsed = parseLintoSegmentId(segment.id)
        if (!parsed) continue // not a LinTO segment (e.g. native agent)
        if (parsed.lang) {
          upsertTranslation(parsed.base, parsed.lang, segment.text, receivedAt)
          continue
        }
        // Attributed lines resolve to a real participant; unattributed ones are
        // published by the (hidden) bot, which the client cannot resolve.
        const identity = participant?.identity
        const isBot = !identity || identity.startsWith('linto-visio-bot-')
        upsertCaption({
          id: segment.id,
          text: segment.text,
          locutor:
            !isBot && participant
              ? getParticipantName(participant)
              : UNATTRIBUTED_SPEAKER,
          participantIdentity: isBot ? undefined : identity,
          language: segment.language || undefined,
          startTime: segment.startTime,
          partial: !segment.final,
          receivedAt,
        })
      }
    }

    room.on(RoomEvent.TranscriptionReceived, onTranscription)
    return () => {
      room.off(RoomEvent.TranscriptionReceived, onTranscription)
    }
  }, [room])

  useEffect(() => {
    if (!active) clearTranscript()
  }, [active])
}
