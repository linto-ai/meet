import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Route, Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'
import { Conference } from '@/features/rooms/components/Conference'
import { Button } from '@/primitives'
import { EmbedIdleContext } from '../EmbedIdleContext'
import {
  type IntentFailure,
  type IntentJoin,
  startIntentService,
} from '../intentService'
import type { HostBridge } from '../embedEvents'
import { useCallEvents, useFinish } from '../useEmbedCall'

/**
 * Connects to the host (the intent of the URL) for the life of the component
 * (an Effect: the service is a listener on `window`, outside React). Returns
 * what the page shows or does: the join request, the leave flag, a failure to
 * display, a sign-in to offer, and a ref to the bridge to talk back.
 * Used by: IntentService.
 */
const useIntentHost = () => {
  const [join, setJoin] = useState<IntentJoin | null>(null)
  const [isLeaving, setIsLeaving] = useState(false)
  // Set in the handler itself: a LiveKit callback running before the next
  // render must already see the leave
  const isLeavingRef = useRef(false)
  const [failure, setFailure] = useState<IntentFailure | null>(null)
  const [signIn, setSignIn] = useState<(() => void) | null>(null)
  const bridge = useRef<HostBridge | null>(null)

  useEffect(() => {
    const host = startIntentService({
      onJoin: (request) => {
        setSignIn(null)
        setJoin(request)
      },
      onLeave: () => {
        isLeavingRef.current = true
        setIsLeaving(true)
      },
      onError: (code) => {
        setSignIn(null)
        setFailure(code)
      },
      // a function in state: wrapped so React does not call it
      onNeedsSignIn: (start) => setSignIn(() => start),
    })
    bridge.current = host
    return () => host?.close()
  }, [])

  return { join, isLeaving, isLeavingRef, failure, signIn, bridge }
}

/**
 * The in-call UI reads the room from the route (`useParams`), and /intents
 * has none: the room comes in the data of the host. A router of its own
 * fixed on /<room> gives it back, without leaving /intents in the address bar.
 */
const InRoom = ({ room, children }: { room: string; children: ReactNode }) => {
  const { hook } = useMemo(
    () => memoryLocation({ path: `/${room}`, static: true }),
    [room]
  )
  return (
    <Router hook={hook}>
      <Route path="/:roomId">{children}</Route>
    </Router>
  )
}

/**
 * Meet as the service of a cozy intent: waits for the data of the host, then
 * enters the call with the LiveKit token of the host (the Meet session, if
 * any, only serves to exchange the platform token). It
 * only shows a sign-in button (no session) or an error that no service could
 * tell the host; nothing once the call is over.
 */
const IntentService = () => {
  const { t } = useTranslation()
  const { join, isLeaving, isLeavingRef, failure, signIn, bridge } =
    useIntentHost()
  const { isOver, finish, onIdle } = useFinish(bridge)
  const events = useCallEvents(bridge, isLeaving, isLeavingRef, finish)

  if (failure) return <p role="alert">{t(`embed.intent.error.${failure}`)}</p>
  if (signIn)
    return <Button onPress={signIn}>{t('embed.intent.signIn')}</Button>
  if (!join || isOver) return null

  const { room, ...call } = join
  return (
    <InRoom room={room}>
      <EmbedIdleContext.Provider value={onIdle}>
        <Conference
          roomId={room}
          onLobbyRequired={() => {}}
          embed={{ ...call, isLeaving, ...events }}
        />
      </EmbedIdleContext.Provider>
    </InRoom>
  )
}

export default IntentService
