import { describe, expect, it } from 'vitest'
import { lintoStatusFromMetadata } from './lintoStatus'

const ACTIVE = {
  linto_transcription_status: 'active',
  linto_transcription_started_by: 'user-1',
  linto_transcription_session_id: 'sess-1',
  linto_transcription_channel_id: 'chan-1',
  linto_transcription_channel_index: 0,
  linto_transcription_org_id: 'org-1',
  linto_transcription_started_at: '2026-11-09T10:00:00Z',
  linto_transcription_catchup: '1',
}

describe('lintoStatusFromMetadata', () => {
  it('reads an active run that keeps the meeting audio', () => {
    expect(
      lintoStatusFromMetadata(true, {
        ...ACTIVE,
        linto_transcription_keep_audio: '1',
      })
    ).toEqual({
      active: true,
      startedBy: 'user-1',
      sessionId: 'sess-1',
      channelId: 'chan-1',
      channelIndex: 0,
      orgId: 'org-1',
      startedAt: '2026-11-09T10:00:00Z',
      catchUpEnabled: true,
      keepsAudio: true,
    })
  })

  it('a text-only run keeps no audio', () => {
    const status = lintoStatusFromMetadata(true, {
      ...ACTIVE,
      linto_transcription_keep_audio: '0',
    })
    expect(status.active).toBe(true)
    expect(status.keepsAudio).toBe(false)
  })

  it('a run that published no keep-audio key keeps no audio', () => {
    const status = lintoStatusFromMetadata(true, ACTIVE)
    expect(status.active).toBe(true)
    expect(status.keepsAudio).toBe(false)
  })

  it('no run, no kept audio', () => {
    // Stopped: the backend removed every LinTO key.
    expect(lintoStatusFromMetadata(true, undefined).keepsAudio).toBe(false)
    expect(lintoStatusFromMetadata(true, {}).keepsAudio).toBe(false)
    // A stray key without an active status lights nothing.
    expect(
      lintoStatusFromMetadata(true, { linto_transcription_keep_audio: '1' })
        .keepsAudio
    ).toBe(false)
  })

  it('the feature off lights nothing', () => {
    const status = lintoStatusFromMetadata(false, {
      ...ACTIVE,
      linto_transcription_keep_audio: '1',
    })
    expect(status.active).toBe(false)
    expect(status.keepsAudio).toBe(false)
  })

  it('defaults the late-joiner summary on for older runs', () => {
    const older: Record<string, unknown> = { ...ACTIVE }
    delete older.linto_transcription_catchup
    expect(lintoStatusFromMetadata(true, older).catchUpEnabled).toBe(true)
  })
})
