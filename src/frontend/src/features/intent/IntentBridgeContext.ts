import { createContext, useContext } from 'react'
import type { HostBridge } from './intentEvents'

/**
 * The bridge to the host of /intents (joined / left / error), given by the
 * IntentService page once the service started; null before and on every
 * other page. A context, so the hooks of the call read it instead of taking
 * it (or a ref to it) as an argument.
 * Used by: IntentService (provides), useFinish and useCallEvents (read).
 */
export const IntentBridgeContext = createContext<HostBridge | null>(null)

/** The bridge to the host, or null when there is none. */
export const useIntentBridge = () => useContext(IntentBridgeContext)
