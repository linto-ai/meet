import {
  type ReactNode,
  type RefObject,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useTranslation } from 'react-i18next'
import { Route, Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'
import { Conference } from '@/features/rooms/components/Conference'
import { Permissions } from '@/features/rooms/components/Permissions'
import { IntentIdleContext } from '../IntentIdleContext'
import { IntentBridgeContext } from '../IntentBridgeContext'
import {
  type IntentFailure,
  type IntentJoin,
  startIntentService,
} from '../intentService'
import type { HostBridge } from '../intentEvents'
import { useCallEvents, useFinish } from '../useIntentCall'

/**
 * Connects to the host (the intent of the URL) for the life of the component
 * (an Effect: the service is a listener on `window`, outside React). Returns
 * what the page shows or does: the call (the join request and the bridge to
 * talk back), the leave flag and a failure to display.
 * Used by: IntentService.
 */
const useIntentHost = () => {
  const [call, setCall] = useState<{
    join: IntentJoin
    bridge: HostBridge
  } | null>(null)
  const [isLeaving, setIsLeaving] = useState(false)
  // Set in the handler itself: a LiveKit callback running before the next
  // render must already see the leave
  const isLeavingRef = useRef(false)
  const [failure, setFailure] = useState<IntentFailure | null>(null)

  useEffect(() => {
    const host = startIntentService({
      // onJoin comes after the awaits of the service: `host` is set by then
      onJoin: (join) => {
        if (host) setCall({ join, bridge: host })
      },
      onLeave: () => {
        isLeavingRef.current = true
        setIsLeaving(true)
      },
      onError: setFailure,
    })
    return () => host?.close()
  }, [])

  return { call, isLeaving, isLeavingRef, failure }
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
 * The call the host asked for, with its LiveKit token (the Meet session, if
 * any, only serves to exchange the platform token). Rendered inside
 * IntentBridgeContext, which its hooks read to tell the host; nothing once
 * the call is over. Used by: IntentCall.
 */
const InCall = ({
  join,
  isLeaving,
  isLeavingRef,
}: {
  join: IntentJoin
  isLeaving: boolean
  isLeavingRef: RefObject<boolean>
}) => {
  const { isOver, finish, onIdle } = useFinish()
  const events = useCallEvents(isLeaving, isLeavingRef, finish)
  if (isOver) return null

  const { room, ...options } = join
  return (
    <InRoom room={room}>
      <IntentIdleContext.Provider value={onIdle}>
        <Conference
          roomId={room}
          onLobbyRequired={() => {}}
          intent={{ ...options, isLeaving, ...events }}
        />
      </IntentIdleContext.Provider>
    </InRoom>
  )
}

/**
 * Waits for the data of the host, then enters the call. It only shows an
 * error that no service could tell the host; never a sign-in: the session
 * exists before the host frames /intents.
 * Used by: IntentService.
 */
const IntentCall = () => {
  const { t } = useTranslation()
  const { call, isLeaving, isLeavingRef, failure } = useIntentHost()

  if (failure) return <p role="alert">{t(`intent.error.${failure}`)}</p>
  if (!call) return null

  return (
    <IntentBridgeContext.Provider value={call.bridge}>
      <InCall
        join={call.join}
        isLeaving={isLeaving}
        isLeavingRef={isLeavingRef}
      />
    </IntentBridgeContext.Provider>
  )
}

/**
 * Meet as the service of a cozy intent (the /intents page).
 * Mounts `Permissions` beside the call, as the room route does: it watches
 * the camera and microphone permissions from the first render (without it
 * the store stays loading and the device toggles show "permission needed"),
 * and draws the dialog a blocked device opens.
 */
const IntentService = () => (
  <>
    <Permissions />
    <IntentCall />
  </>
)

export default IntentService
