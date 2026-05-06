import { useTitle } from 'hoofd'
import { useRoomData } from './useRoomData'
import { useMemo } from 'react'
import { getEnv } from '@/utils/getEnv'

/**
 * Updates the browser tab title with the room name to help users easily find
 * the meeting tab among many open tabs. Works on both the join screen and
 * once connected.
 */
export const useRoomPageTitle = () => {
  const roomData = useRoomData()

  const pageTitle = useMemo(() => {
    const appTitle = getEnv('VITE_APP_TITLE') ?? ''
    if (!roomData) {
      return appTitle
    }

    const roomLabel = roomData.name || roomData.slug || ''

    if (!roomLabel) return appTitle

    return `${appTitle} - ${roomLabel}  `
  }, [roomData])

  useTitle(pageTitle)
}
