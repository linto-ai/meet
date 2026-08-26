import { useSnapshot } from 'valtio'
import { layoutStore } from '@/stores/layout'
import { useStartSubtitle } from '../api/startSubtitle'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { useRoomContext } from '@livekit/components-react'
import { useEffect, useRef } from 'react'
import { RoomEvent } from 'livekit-client'
import { useLintoStatus } from '@/features/transcription-bot/hooks/useLintoStatus'

export const useSubtitles = () => {
  const layoutSnap = useSnapshot(layoutStore)

  const room = useRoomContext()
  const apiRoomData = useRoomData()
  const { mutateAsync: startSubtitleRoom, isPending } = useStartSubtitle()
  // LinTO live transcription (fork): while the LinTO bot transcribes the room
  // the overlay is fed by ITS segments — never dispatch the native agent too.
  const { active: isLintoActive } = useLintoStatus()

  const toggleSubtitles = async () => {
    if (!layoutSnap.showSubtitles && !isLintoActive && apiRoomData?.livekit) {
      await startSubtitleRoom({
        id: apiRoomData?.livekit?.room,
        token: apiRoomData?.livekit?.token,
      })
    }

    layoutStore.showSubtitles = !layoutSnap.showSubtitles
  }

  useEffect(() => {
    if (!room) return

    const closeSubtitles = () => {
      layoutStore.showSubtitles = false
    }
    room.on(RoomEvent.Disconnected, closeSubtitles)
    return () => {
      room.off(RoomEvent.Disconnected, closeSubtitles)
    }
  }, [room])

  // Open the overlay ONCE when LinTO takes over the captions (a later manual
  // close is respected; a new run re-opens it).
  const autoOpenedRef = useRef(false)
  useEffect(() => {
    if (!isLintoActive) {
      autoOpenedRef.current = false
      return
    }
    if (autoOpenedRef.current) return
    autoOpenedRef.current = true
    layoutStore.showSubtitles = true
  }, [isLintoActive])

  return {
    areSubtitlesOpen: layoutSnap.showSubtitles,
    toggleSubtitles,
    areSubtitlesPending: isPending,
    isLintoActive,
  }
}
