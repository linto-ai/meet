import { useConfig } from '@/api/useConfig'
import { useAnalytics } from '@/features/analytics/hooks/useAnalytics'
import { useSyncUserPreferencesWithBackend } from '@/features/auth/api/useSyncUserPreferencesWithBackend'
import { useEffect } from 'react'
import { CozyBridge } from 'cozy-external-bridge'
import { getEnv } from '@/utils/getEnv'
import { isEmbedPage } from '@/features/embed/isEmbedPage'

const isOriginAllowed = (parentOrigin: string): boolean => {
  const allowlist = getEnv('VITE_BRIDGE_TARGET_ORIGIN_ALLOWLIST')
  if (!allowlist) return false
  return allowlist
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .some((allowed) => parentOrigin.endsWith(allowed))
}

export const AppInitialization = () => {
  const { data } = useConfig()
  // No cookies, consent or cozy bridge in the frame of another site
  const isEmbed = isEmbedPage()
  useSyncUserPreferencesWithBackend()

  const { analytics = {}, custom_css_url = '' } = data ?? {}

  useAnalytics(isEmbed ? { isDisabled: true } : analytics)

  useEffect(() => {
    if (isEmbed) return
    const setupBridge = async () => {
      const bridge = new CozyBridge()
      if (!bridge.isInIframe()) return

      const parentOrigin = await bridge.requestParentOrigin()
      if (!parentOrigin || !isOriginAllowed(parentOrigin)) return

      bridge.setupBridge(parentOrigin)
      bridge.startHistorySyncing()
      window.twake = { twakeOrigin: `${parentOrigin}/#/bridge` }
    }
    setupBridge()
  }, [isEmbed])

  useEffect(() => {
    if (custom_css_url) {
      const link = document.createElement('link')
      link.href = custom_css_url
      link.id = 'meet-custom-css'
      link.rel = 'stylesheet'
      document.head.appendChild(link)
    }
  }, [custom_css_url])

  return null
}
