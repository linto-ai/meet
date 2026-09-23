import { toastQueue } from './components/ToastProvider'
import { NotificationType } from './NotificationType'
import { NotificationDuration } from './NotificationDuration'
import type { Participant } from 'livekit-client'
import type { NotificationPayload } from './NotificationPayload'
import type { RecordingMode } from '@/features/recording'
import { reportError } from '@/features/analytics/telemetry'

export const notifyAutoMutedOnJoin = () => {
  toastQueue.add(
    {
      type: NotificationType.AutoMuteLargeRoom,
    },
    { timeout: NotificationDuration.ALERT }
  )
}

export const showLowerHandToast = (
  participant: Participant,
  onClose: () => void
) => {
  toastQueue.add(
    {
      participant,
      type: NotificationType.LowerHand,
    },
    {
      timeout: NotificationDuration.LOWER_HAND,
      onClose,
    }
  )
}

export const closeLowerHandToasts = () => {
  toastQueue.visibleToasts.forEach((toast) => {
    if (toast.content.type === NotificationType.LowerHand) {
      toastQueue.close(toast.key)
    }
  })
}

export const decodeNotificationDataReceived = (
  payload: Uint8Array
): NotificationPayload | undefined => {
  if (!payload || !(payload instanceof Uint8Array)) {
    throw new Error('Invalid payload: expected Uint8Array')
  }
  try {
    const decoder = new TextDecoder()
    const jsonString = decoder.decode(payload)
    if (!jsonString || typeof jsonString !== 'string') {
      throw new Error('Invalid decoded content')
    }
    // Parse with additional validation if needed
    const parsed = JSON.parse(jsonString)
    return parsed as NotificationPayload
  } catch (error) {
    // Handle errors appropriately for your application
    reportError('generic_failure', error, {
      context: 'Failed to decode notification payload:',
    })
    return
  }
}

export const notifyRecordingSaveInProgress = (
  mode: RecordingMode,
  participant: Participant
) => {
  toastQueue.add(
    {
      participant,
      mode,
      type: NotificationType.RecordingSaving,
    },
    { timeout: NotificationDuration.RECORDING_SAVING }
  )
}

/**
 * LinTO "the summary is on its way" toast (fork). Carries everything the
 * renderer needs: `startedByMe` decides whether the email recipient is "me"
 * (my own address, bold) or the generic organizer wording. Triggered from the
 * LinTO panel's stop handler when the summary add-on was ON.
 */
export const notifyLintoSummarySaving = (
  startedByMe: boolean,
  email?: string
) => {
  toastQueue.add(
    {
      type: NotificationType.LintoSummarySaving,
      startedByMe,
      email,
    },
    { timeout: NotificationDuration.RECORDING_SAVING }
  )
}

/**
 * LinTO "what was said before you arrived is ready" toast (fork): shown to a
 * late joiner whose panel is closed when the catch-up summary lands. Pressing
 * it opens the live transcription panel (`open`); it goes away by itself.
 */
export const notifyLintoCatchUpReady = (open: () => void) => {
  toastQueue.add(
    {
      type: NotificationType.LintoCatchUpReady,
      onOpen: open,
    },
    { timeout: NotificationDuration.RECORDING_SAVING }
  )
}
