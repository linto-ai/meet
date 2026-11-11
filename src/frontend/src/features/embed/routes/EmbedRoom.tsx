import { type RefObject, useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'wouter'
import { Conference } from '@/features/rooms/components/Conference'
import { EmbedIdleContext } from '../EmbedIdleContext'
import { toErrorCode, toLeftReason } from '../embedEvents'
import {
  type HostBridge,
  type HostJoin,
  type LeftReason,
  startHostBridge,
} from '../hostBridge'

// Without a join by then, the parent does not speak this protocol
const JOIN_TIMEOUT_MS = 10_000

/**
 * Connects to the parent page for the life of the component (an Effect: the
 * bridge is a listener on `window`, outside React). Returns the join request
 * once received, the leave flag, and a ref to the bridge to talk back.
 * Used by: EmbedRoom.
 */
const useHost = () => {
  const [join, setJoin] = useState<HostJoin | null>(null)
  const [isLeaving, setIsLeaving] = useState(false)
  const bridge = useRef<HostBridge | null>(null)

  useEffect(() => {
    const host = startHostBridge({
      onJoin: (request) => {
        clearTimeout(timer)
        setJoin(request)
      },
      onLeave: () => setIsLeaving(true),
    })
    bridge.current = host
    const timer = setTimeout(() => {
      host?.error('unsupported')
      host?.close() // a join after the error is ignored
    }, JOIN_TIMEOUT_MS)
    return () => {
      clearTimeout(timer)
      host?.close()
    }
  }, [])

  return { join, isLeaving, bridge }
}

/**
 * Tells the parent once that the call is over, then flags it so the call
 * unmounts. `onIdle` is stable: the idle countdown restarts when it changes.
 * Used by: EmbedRoom.
 */
const useFinish = (bridge: RefObject<HostBridge | null>) => {
  const [isOver, setIsOver] = useState(false)
  const isOverRef = useRef(false)

  const finish = useCallback(
    (reason: LeftReason) => {
      if (isOverRef.current) return
      isOverRef.current = true
      bridge.current?.left(reason)
      setIsOver(true)
    },
    [bridge]
  )
  const onIdle = useCallback(() => finish('idle'), [finish])

  return { isOver, finish, onIdle }
}

/**
 * Meet in an iframe of Chat: waits for the `join` message of the parent, then
 * enters the call with its LiveKit token. No Meet session, no pre-join
 * screen, nothing shown once the call is over.
 */
const EmbedRoom = () => {
  const { roomId } = useParams()
  const { join, isLeaving, bridge } = useHost()
  const { isOver, finish, onIdle } = useFinish(bridge)
  // LiveKit also disconnects after a join that failed, which is an `error`,
  // not a `left`: `left` only has a meaning once `joined` was said
  const isJoined = useRef(false)

  if (!roomId || !join || isOver) return null

  return (
    <EmbedIdleContext.Provider value={onIdle}>
      <Conference
        roomId={roomId}
        onLobbyRequired={() => {}}
        embed={{
          ...join,
          isLeaving,
          onConnected: () => {
            isJoined.current = true
            bridge.current?.joined()
          },
          onDisconnected: (reason) => {
            if (isJoined.current) finish(toLeftReason(reason))
          },
          onError: (error) => bridge.current?.error(toErrorCode(error)),
        }}
      />
    </EmbedIdleContext.Provider>
  )
}

export default EmbedRoom
