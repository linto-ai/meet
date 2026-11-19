import { type RefObject, useCallback, useEffect, useRef, useState } from 'react'
import { type DisconnectReason } from 'livekit-client'
import {
  type ErrorCode,
  type HostBridge,
  type LeftReason,
  toErrorCode,
  toLeftReason,
} from './embedEvents'

/*
 * What /intents needs once the host has asked for a call: the finish-once
 * flag, the grace of a bare disconnection and the LiveKit events that make
 * the host speak. They only need a bridge (joined / left / error).
 */

// A bare disconnection before `joined` waits this long for the precise
// ConnectionError, which reaches us later by the network
const DISCONNECT_GRACE_MS = 1_000

/**
 * Tells the parent once that the call is over, then flags it so the call
 * unmounts. `onIdle` is stable: the idle countdown restarts when it changes.
 * Used by: IntentService.
 */
export const useFinish = (bridge: RefObject<HostBridge | null>) => {
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
 * a timer lives outside React). Used by: IntentService.
 */
export const useGrace = (isLeaving: boolean) => {
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
 * The LiveKit events that make the parent speak: `joined` once connected,
 * `left` after it, `error` for a join that failed (a bare disconnection waits
 * the grace for the precise error). Nothing is said about a connection the
 * host abandoned with `leave`. Used by: IntentService.
 */
export const useCallEvents = (
  bridge: RefObject<HostBridge | null>,
  isLeaving: boolean,
  isLeavingRef: RefObject<boolean>,
  finish: (reason: LeftReason) => void
) => {
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

  return {
    onConnected: () => {
      // The host left while the connection was under way: not joined
      if (isLeavingRef.current) return
      isJoined.current = true
      cancelGrace()
      bridge.current?.joined()
    },
    onDisconnected: (reason?: DisconnectReason) => {
      if (isJoined.current) return finish(toLeftReason(reason))
      // The host asked for it (leave) before `joined`: nothing to report
      if (isLeavingRef.current) return
      // A disconnection with no ConnectionError is a join that failed:
      // wait a grace period (one, a second disconnection does not
      // restart it) for the precise onError that may follow
      startGrace(() => postError('unreachable'))
    },
    onError: (error: Error) => postError(toErrorCode(error)),
  }
}
