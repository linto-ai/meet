import { createContext, useContext } from 'react'

/**
 * Set by /intents: what to do when the user is disconnected for being
 * idle, in place of going to /feedback. Null on every other page.
 * Used by: IntentService (provides), IsIdleDisconnectModal (reads).
 */
export const IntentIdleContext = createContext<(() => void) | null>(null)

/** The idle leave of /intents, or null elsewhere. */
export const useIntentIdleLeave = () => useContext(IntentIdleContext)
