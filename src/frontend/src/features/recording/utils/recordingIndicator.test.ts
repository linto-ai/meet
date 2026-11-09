import { describe, expect, it } from 'vitest'
import {
  LINTO_KEEP_AUDIO_INDICATOR_KEY,
  resolveRecordingIndicator,
  type RecordingIndicatorInput,
} from './recordingIndicator'

const IDLE: RecordingIndicatorInput = {
  recordingMode: undefined,
  recordingStatus: undefined,
  isStarting: false,
  isStarted: false,
  isRecording: false,
  lintoKeepsAudio: false,
}

describe('resolveRecordingIndicator', () => {
  it('shows nothing when nothing records', () => {
    expect(resolveRecordingIndicator(IDLE)).toBeUndefined()
  })

  it('shows the generic recording wording for a live run keeping the audio', () => {
    expect(
      resolveRecordingIndicator({ ...IDLE, lintoKeepsAudio: true })
    ).toEqual({ key: LINTO_KEEP_AUDIO_INDICATOR_KEY, source: 'linto' })
    expect(LINTO_KEEP_AUDIO_INDICATOR_KEY).toBe('any.started')
  })

  it('keeps the egress indicator unchanged', () => {
    expect(
      resolveRecordingIndicator({
        ...IDLE,
        recordingMode: 'transcript',
        recordingStatus: 'started',
        isStarted: true,
        isRecording: true,
      })
    ).toEqual({ key: 'transcript.started', source: 'egress' })
    expect(
      resolveRecordingIndicator({
        ...IDLE,
        recordingMode: 'screen_recording',
        recordingStatus: 'starting',
        isStarting: true,
      })
    ).toEqual({ key: 'screen_recording.starting', source: 'egress' })
  })

  it('reports an egress LiveKit does not record yet as starting', () => {
    expect(
      resolveRecordingIndicator({
        ...IDLE,
        recordingMode: 'screen_recording',
        recordingStatus: 'started',
        isStarted: true,
        isRecording: false,
      })
    ).toEqual({ key: 'screen_recording.starting', source: 'egress' })
  })

  it('shows one indicator, the egress one, when both record', () => {
    expect(
      resolveRecordingIndicator({
        ...IDLE,
        recordingMode: 'transcript',
        recordingStatus: 'started',
        isStarted: true,
        isRecording: true,
        lintoKeepsAudio: true,
      })
    ).toEqual({ key: 'transcript.started', source: 'egress' })
  })

  it('falls back to the live run once the egress is no longer active', () => {
    // The egress is saving (neither starting nor started): the live run still
    // keeps the audio, so the indicator stays up.
    expect(
      resolveRecordingIndicator({
        ...IDLE,
        recordingMode: 'screen_recording',
        recordingStatus: 'saving',
        lintoKeepsAudio: true,
      })
    ).toEqual({ key: LINTO_KEEP_AUDIO_INDICATOR_KEY, source: 'linto' })
  })

  it('ignores a stale egress mode without a status', () => {
    expect(
      resolveRecordingIndicator({
        ...IDLE,
        recordingMode: 'transcript',
        isStarted: true,
      })
    ).toBeUndefined()
  })
})
