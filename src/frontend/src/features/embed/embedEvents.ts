import {
  ConnectionError,
  ConnectionErrorReason,
  DisconnectReason,
} from 'livekit-client'
import type { ErrorCode, LeftReason } from './hostBridge'

/** What the parent is told when LiveKit disconnects the user. */
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

/** What the parent is told when the join fails. */
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
