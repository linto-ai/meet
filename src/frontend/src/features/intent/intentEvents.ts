import {
  ConnectionError,
  ConnectionErrorReason,
  DisconnectReason,
} from 'livekit-client'

/*
 * What Meet tells the host that framed it (Meet is the service of a cozy
 * intent, /intents): joined, left, error.
 */
export type LeftReason = 'user' | 'idle' | 'removed' | 'disconnected'
export type ErrorCode =
  | 'unsupported'
  | 'token_rejected'
  | 'unreachable'
  | 'unauthenticated'
  | 'unknown'

export type HostJoin = { token: string; audio: boolean; video: boolean }

export type HostBridge = {
  joined: () => void
  left: (reason: LeftReason) => void
  error: (code: ErrorCode) => void
  close: () => void
}

/** What the host is told when LiveKit disconnects the user. */
export const toLeftReason = (reason?: DisconnectReason): LeftReason => {
  switch (reason) {
    case DisconnectReason.CLIENT_INITIATED:
      return 'user'
    case DisconnectReason.PARTICIPANT_REMOVED:
    case DisconnectReason.DUPLICATE_IDENTITY:
    case DisconnectReason.ROOM_DELETED:
      return 'removed'
    default:
      return 'disconnected'
  }
}

/** What the host is told when the join fails. */
export const toErrorCode = (error: Error): ErrorCode => {
  if (!(error instanceof ConnectionError)) return 'unknown'
  switch (error.reason) {
    case ConnectionErrorReason.NotAllowed:
      return 'token_rejected'
    case ConnectionErrorReason.ServerUnreachable:
      return 'unreachable'
    default:
      return 'unknown'
  }
}
