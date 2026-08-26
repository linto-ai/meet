import { useEffect } from 'react'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useUser } from '@/features/auth/api/useUser'
import { useLintoBotStatus } from '../api/lintoBotApi'
import { lintoStore } from '../store/lintoStore'
import { hydrateCaptions } from '../store/transcriptStore'
import { useLintoStatus } from './useLintoStatus'

/**
 * Cross-user hydration of the LinTO panel.
 *
 * The room metadata already tells every participant that a transcription runs
 * (useLintoStatus). When the panel is open during a run, this fetches
 * bot-status ONCE to learn the starter / session and to hydrate the finalized
 * lines a late joiner missed (the live feed itself is LiveKit-native).
 */
export const useLintoBotSync = (panelOpen: boolean) => {
  const apiRoomData = useRoomData()
  const roomId = apiRoomData?.livekit?.room
  const token = apiRoomData?.livekit?.token
  const { user } = useUser()
  const currentUserId = user?.id
  const { active } = useLintoStatus()

  const { data } = useLintoBotStatus(roomId, token, panelOpen && active)

  useEffect(() => {
    if (!active || !data) return
    if (data.status !== 'running') return
    if (data.orgId) lintoStore.orgId = data.orgId
    if (data.sessionId) lintoStore.sessionId = data.sessionId
    if (data.userId) {
      lintoStore.userId = data.userId
      // Never downgrade a locally-set `startedByMe`; only promote on a match.
      if (currentUserId && data.userId === String(currentUserId)) {
        lintoStore.startedByMe = true
      }
    }
    if (data.captions.length > 0) hydrateCaptions(data.captions)
  }, [active, data, currentUserId])
}
