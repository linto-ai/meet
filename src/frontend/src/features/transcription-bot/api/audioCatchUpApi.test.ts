import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  applyAudioCatchUp,
  getAudioCatchUp,
  requestAudioCatchUp,
} from './audioCatchUpApi'
import { IDLE_AUDIO_CATCH_UP } from '../store/audioCatchUpStore'

const ROOM = 'room-1'
const TOKEN = 'lk-token'

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

const lastCall = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls[0] as unknown as [string, RequestInit]

beforeEach(() => {
  vi.stubGlobal('document', { cookie: 'csrftoken=csrf-1' })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('audio catch-up requests', () => {
  it('asks for the brief with the LiveKit token in the header only', async () => {
    const fetchMock = vi.fn(async () =>
      json(200, { state: 'sending', recording_id: 'r1' })
    )
    vi.stubGlobal('fetch', fetchMock)
    await requestAudioCatchUp(ROOM, TOKEN, { joinedAtMs: 1000 })

    const [url, init] = lastCall(fetchMock)
    expect(url).toBe(`/api/v1.0/rooms/${ROOM}/linto/audio-catchup/`)
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>).Authorization).toBe(
      `X-LiveKit-Token ${TOKEN}`
    )
    expect(JSON.parse(init.body as string)).toEqual({ joined_at_ms: 1000 })
  })

  it('reads the brief of a recording without the token in the URL', async () => {
    const fetchMock = vi.fn(async () => json(200, { state: 'queued' }))
    vi.stubGlobal('fetch', fetchMock)
    await getAudioCatchUp(ROOM, TOKEN, 'rec')

    const [url, init] = lastCall(fetchMock)
    expect(url).toBe(
      `/api/v1.0/rooms/${ROOM}/linto/audio-catchup/?recording_id=rec`
    )
    expect(url).not.toContain(TOKEN)
    expect((init.headers as Record<string, string>).Authorization).toBe(
      `X-LiveKit-Token ${TOKEN}`
    )
  })
})

describe('applyAudioCatchUp', () => {
  it('stays idle when a live run serves the late joiners', () => {
    const next = applyAudioCatchUp(IDLE_AUDIO_CATCH_UP, { state: 'live' })
    expect(next.status).toBe('idle')
    expect(next.brief).toBeNull()
  })

  it('keeps the brief and its coverage once ready', () => {
    const next = applyAudioCatchUp(IDLE_AUDIO_CATCH_UP, {
      state: 'ready',
      recording_id: 'rec',
      brief: {
        text: '  Points clés  ',
        covered_until: '2026-11-15T10:00:00Z',
      },
    })
    expect(next.status).toBe('ready')
    expect(next.progress).toBe(1)
    expect(next.brief?.text).toBe('Points clés')
    expect(next.brief?.coveredUntil).toBe(Date.parse('2026-11-15T10:00:00Z'))
  })

  it('clamps the progress', () => {
    const next = applyAudioCatchUp(IDLE_AUDIO_CATCH_UP, {
      state: 'transcribing',
      progress: 3,
    })
    expect(next.progress).toBe(1)
  })

  it('keeps the step, its progress and the estimate while it runs', () => {
    const next = applyAudioCatchUp(IDLE_AUDIO_CATCH_UP, {
      state: 'transcribing',
      recording_id: 'rec',
      progress: 0.4,
      eta_s: [30, 90],
    })
    expect(next.status).toBe('transcribing')
    expect(next.recordingId).toBe('rec')
    expect(next.progress).toBe(0.4)
    expect(next.etaS).toEqual([30, 90])
    expect(next.overdue).toBe(false)
  })

  it('flags a brief the server could not update, with the reason', () => {
    const next = applyAudioCatchUp(IDLE_AUDIO_CATCH_UP, {
      state: 'ready',
      stale: true,
      reason: 'summary_service',
      brief: { text: 'Old', covered_until: '2026-11-15T10:00:00Z' },
    })
    expect(next.stale).toBe(true)
    expect(next.error).toBe('summary_service')
  })

  it('reads a recording ended while its brief was prepared as stopped', () => {
    const running = applyAudioCatchUp(IDLE_AUDIO_CATCH_UP, {
      state: 'transcribing',
      recording_id: 'rec',
    })
    expect(applyAudioCatchUp(running, { state: 'none' })).toMatchObject({
      status: 'failed',
      stale: false,
      error: 'stopped',
    })
    const withBrief = { ...running, brief: { text: 'Old', coveredUntil: 1 } }
    expect(applyAudioCatchUp(withBrief, { state: 'none' })).toMatchObject({
      status: 'ready',
      stale: true,
      error: 'stopped',
      brief: { text: 'Old' },
    })
  })

  it('says when a step runs past its estimate', () => {
    const next = applyAudioCatchUp(IDLE_AUDIO_CATCH_UP, {
      state: 'transcribing',
      eta_s: null,
      overdue: true,
    })
    expect(next.etaS).toBeNull()
    expect(next.overdue).toBe(true)
  })
})
