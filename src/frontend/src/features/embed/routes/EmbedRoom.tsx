import { type RefObject, useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'wouter'
import { Conference } from '@/features/rooms/components/Conference'
import { EmbedIdleContext } from '../EmbedIdleContext'
import { toErrorCode, toLeftReason } from '../embedEvents'
import {
  type HostBridge,
  type ErrorCode,
  type HostJoin,
  type LeftReason,
  startHostBridge,
} from '../hostBridge'

// Without a join by then, the parent does not speak this protocol
const JOIN_TIMEOUT_MS = 10_000

// A bare disconnection before `joined` waits this long for the precise
// ConnectionError, which reaches us later by the network
const DISCONNECT_GRACE_MS = 1_000

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
 * The grace period of a bare disconnection before `joined`: `startGrace` arms
 * one timer (a second call does not restart it), `cancelGrace` clears it. The
 * timer is also cleared on unmount and once the host asked to leave (an Effect:
 * a timer lives outside React). Used by: EmbedRoom.
 */
const useGrace = (isLeaving: boolean) => {
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const cancelGrace = () => {
    clearTimeout(timer.current)
    timer.current = undefined
  }
  const startGrace = (onExpire: () => void) => {
    timer.current ??= setTimeout(onExpire, DISCONNECT_GRACE_MS)
  }
  useEffect(() => {
    if (isLeaving) cancelGrace()
    return cancelGrace
  }, [isLeaving])
  return { startGrace, cancelGrace }
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
  // One `error` per failed join: set by the first one posted
  const isErrorPosted = useRef(false)

  const { startGrace, cancelGrace } = useGrace(isLeaving)

  const postError = (code: ErrorCode) => {
    cancelGrace()
    if (isErrorPosted.current) return
    isErrorPosted.current = true
    bridge.current?.error(code)
  }

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
            cancelGrace()
            bridge.current?.joined()
          },
          onDisconnected: (reason) => {
            if (isJoined.current) return finish(toLeftReason(reason))
            // The host asked for it (leave): nothing to report
            if (isLeaving) return
            // A disconnection with no ConnectionError is a join that failed:
            // wait a grace period (one, a second disconnection does not
            // restart it) for the precise onError that may follow
            startGrace(() => postError('unreachable'))
          },
          onError: (error) => postError(toErrorCode(error)),
        }}
      />
    </EmbedIdleContext.Provider>
  )
}

export default EmbedRoom
