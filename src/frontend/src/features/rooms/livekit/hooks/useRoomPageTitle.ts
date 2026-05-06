import { useTitle } from 'hoofd'
import { useMemo } from 'react'
import { getEnv } from '@/utils/getEnv'

/**
 * Updates the browser tab title with the room id to help users easily find
 * the meeting tab among many open tabs. Works on both the join screen and
 * once connected.
 */
export const useRoomPageTitle = (roomId?: string) => {
  const pageTitle = useMemo(() => {
    const appTitle = getEnv('VITE_APP_TITLE') ?? ''
    if (!roomId) return appTitle
    return `${appTitle} - ${roomId}`
  }, [roomId])

  useTitle(pageTitle)
}
