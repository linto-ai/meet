import { useEffect, useMemo } from 'react'
import { useRoomMetadata } from '@/features/recording/hooks/useRoomMetadata'
import { useUser } from '@/features/auth/api/useUser'
import { useLintoConfig } from './useLintoConfig'
import { lintoStore, resetLintoRun } from '../store/lintoStore'
import {
  LINTO_METADATA_STARTER_KEY,
  LINTO_METADATA_STATUS_KEY,
} from '../types/linto'

export interface LintoStatus {
  // The feature is enabled AND a bot currently transcribes this room.
  active: boolean
  // Django id of the participant who started it ('' when anonymous/unknown).
  startedBy: string
}

/**
 * Room-wide LinTO transcription state, read from the LiveKit room metadata the
 * backend writes at start/stop (replayed by LiveKit to late joiners). This is
 * the shared source of truth for the banner, the CC badge and the panel.
 */
export const useLintoStatus = (): LintoStatus => {
  const { enabled } = useLintoConfig()
  const metadata = useRoomMetadata()
  return useMemo(
    () => ({
      active: enabled && metadata?.[LINTO_METADATA_STATUS_KEY] === 'active',
      startedBy: String(metadata?.[LINTO_METADATA_STARTER_KEY] ?? ''),
    }),
    [enabled, metadata]
  )
}

/**
 * Mirror the room-wide state into `lintoStore` (running / startedByMe) so every
 * participant's panel — including one who never clicked Start — reflects it.
 * A short guard after a LOCAL start/stop keeps a stale metadata roundtrip from
 * clobbering the optimistic state. Mount once (LintoProvider).
 */
export const useSyncLintoStatus = () => {
  const { active, startedBy } = useLintoStatus()
  const { user } = useUser()
  const currentUserId = user?.id

  useEffect(() => {
    if (Date.now() < lintoStore.localActionUntil) return
    if (active) {
      if (!lintoStore.running) lintoStore.running = true
      if (startedBy) {
        lintoStore.userId = startedBy
        // Never downgrade a locally-set `startedByMe`; only promote on a match.
        if (currentUserId && startedBy === String(currentUserId)) {
          lintoStore.startedByMe = true
        }
      }
    } else if (lintoStore.running) {
      resetLintoRun()
    }
  }, [active, startedBy, currentUserId])
}
