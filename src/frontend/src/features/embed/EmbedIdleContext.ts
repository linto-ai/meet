import { createContext, useContext } from 'react'

/**
 * Set by /embed/<uuid>: what to do when the user is disconnected for being
 * idle, in place of going to /feedback. Null on every other page.
 */
export const EmbedIdleContext = createContext<(() => void) | null>(null)

export const useEmbedIdleLeave = () => useContext(EmbedIdleContext)
