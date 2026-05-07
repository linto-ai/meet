import { useStartRecording, useStopRecording } from '@/features/recording'
import { recordingStore } from '@/stores/recording'
import { captureEvent } from '@/features/analytics/telemetry'

export const useMutateRecording = () => {
  const { mutateAsync: startRecording, isPending: isPendingToStart } =
    useStartRecording({
      onSuccess: () => {
        recordingStore.startedByMe = true
      },
      onError: () => {
        recordingStore.isErrorDialogOpen = 'start'
        captureEvent('error-starting-recording')
      },
    })
  const { mutateAsync: stopRecording, isPending: isPendingToStop } =
    useStopRecording({
      // Don't reset startedByMe here — the toast that reads this flag is
      // queued AFTER stopRecording resolves, so resetting here would always
      // make the toast fall back to the default (emailless) message.
      // startedByMe is in-memory only and naturally resets on page reload.
      onError: () => {
        recordingStore.isErrorDialogOpen = 'stop'
        captureEvent('error-stopping-recording')
      },
    })

  return {
    startRecording,
    isPendingToStart,
    stopRecording,
    isPendingToStop,
  }
}
