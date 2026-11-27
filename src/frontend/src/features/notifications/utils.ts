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

/** Who the recording goes to, as the stop answered it. */
export type RecordingDelivery = {
  recipientIsMe: boolean
  shared?: boolean
  // Live captions still run beside the stopped recording and I may stop
  // them: the toast offers it.
  stopCaptions?: () => Promise<void>
}

export const notifyRecordingSaveInProgress = (
  mode: RecordingMode,
  participant: Participant,
  delivery?: RecordingDelivery
) => {
  toastQueue.add(
    {
      participant,
      mode,
      type: NotificationType.RecordingSaving,
      recipientIsMe: delivery?.recipientIsMe === true,
      shared: delivery?.shared === true,
      stopCaptions: delivery?.stopCaptions,
    },
    { timeout: NotificationDuration.RECORDING_SAVING }
  )
}

/**
 * LinTO "the recap is on its way" toast (fork), triggered from the LinTO
 * panel's stop handler: `recipientIsMe` addresses it to my own email,
 * otherwise to the meeting organizer; `summary` says whether the recap holds
 * a summary or the transcript alone, `shared` whether the participants get
 * the summary too.
 */
export const notifyLintoSummarySaving = ({
  recipientIsMe,
  email,
  summary = true,
  shared = false,
  noSpeech = false,
}: {
  recipientIsMe: boolean
  email?: string
  summary?: boolean
  shared?: boolean
  noSpeech?: boolean
}) => {
  toastQueue.add(
    {
      type: NotificationType.LintoSummarySaving,
      recipientIsMe,
      email,
      summary,
      shared,
      noSpeech,
    },
    { timeout: NotificationDuration.RECORDING_SAVING }
  )
}

/**
 * LinTO "what was said before you arrived is ready" toast (fork), whatever the
 * brief's source: shown to a late joiner whose brief panel did not open by
 * itself when the brief lands. Pressing it opens the panel (`open`); it goes
 * away by itself.
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
