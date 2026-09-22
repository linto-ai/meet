import { useLintoCapabilities } from './useLintoCapabilities'

export type LintoEntitlement = 'unknown' | 'entitled' | 'no_entitlement'

/**
 * Is the live transcription option active for the current participant?
 * Derived from the capabilities of GET users/me (`linto.transcription.live`),
 * never from a Studio call: a participant without the right never triggers
 * the identity bridge. 'unknown' while the user is still loading; guests are
 * not entitled (they have no identity to bridge).
 */
export const useLintoEntitlement = (): LintoEntitlement => {
  const { loading, live } = useLintoCapabilities()
  if (loading) return 'unknown'
  return live ? 'entitled' : 'no_entitlement'
}
