import type { ApiRoom } from '@/features/rooms/api/ApiRoom'
import { useParams } from 'wouter'
import { useCallback, useSyncExternalStore } from 'react'
import { keys } from '@/api/queryKeys'
import { queryClient } from '@/api/queryClient'

// Read the cached room and re-render when it changes, without registering
// an observer with its own queryFn: such an observer could serve the refetch
// of the room query with a GET, which returns no LiveKit credentials
export const useRoomData = (): ApiRoom | undefined => {
  const { roomId } = useParams()
  const subscribe = useCallback(
    (onStoreChange: () => void) =>
      queryClient.getQueryCache().subscribe(onStoreChange),
    []
  )
  return useSyncExternalStore(subscribe, () =>
    queryClient.getQueryData<ApiRoom>([keys.room, roomId])
  )
}
