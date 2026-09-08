import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useLintoBotProfiles } from '../api/lintoBotApi'

export type LintoEntitlement = 'unknown' | 'entitled' | 'no_entitlement'

/**
 * Is the LinTO option active for the current participant? Derived from the
 * profiles query (shared with the panel, so no extra request): the identity
 * bridge answers "not entitled" when no LinTO key is linked to the user (or
 * the key may not run a quickMeeting). 'unknown' while loading / for guests.
 */
export const useLintoEntitlement = (): LintoEntitlement => {
  const apiRoomData = useRoomData()
  const { data } = useLintoBotProfiles(
    apiRoomData?.livekit?.room,
    apiRoomData?.livekit?.token
  )
  if (!data) return 'unknown'
  return data.reason === 'no_entitlement' ? 'no_entitlement' : 'entitled'
}
