import { describe, expect, it } from 'vitest'
import { catchUpView, isLateArrival } from './catchUpView'
import { IDLE_CATCH_UP, type LintoCatchUpState } from '../store/lintoStore'
import {
  IDLE_AUDIO_CATCH_UP,
  type AudioCatchUpState,
} from '../store/audioCatchUpStore'

const live = (patch: Partial<LintoCatchUpState>): LintoCatchUpState => ({
  ...IDLE_CATCH_UP,
  ...patch,
})
const audio = (patch: Partial<AudioCatchUpState>): AudioCatchUpState => ({
  ...IDLE_AUDIO_CATCH_UP,
  ...patch,
})
const JOINED = Date.parse('2026-11-20T10:10:00Z')

describe('catchUpView', () => {
  it('shows the live brief when a live run of its own serves', () => {
    const view = catchUpView({
      liveServes: true,
      live: live({ status: 'done', text: ' Brief ' }),
      joinedAt: JOINED,
      audio: audio({ status: 'ready', brief: { text: 'x', coveredUntil: 1 } }),
    })
    expect(view).toMatchObject({
      source: 'live',
      status: 'done',
      phase: 'text',
      text: 'Brief',
      joinedAt: JOINED,
    })
  })

  it('shows the audio brief when no live run serves', () => {
    const view = catchUpView({
      liveServes: false,
      live: live({ status: 'done', text: 'live' }),
      joinedAt: JOINED,
      audio: audio({
        status: 'ready',
        brief: { text: 'Audio', coveredUntil: JOINED },
      }),
    })
    expect(view).toMatchObject({
      source: 'audio',
      phase: 'text',
      text: 'Audio',
      coveredUntil: JOINED,
    })
  })

  it('shows the step, its progress and the estimate while the audio runs', () => {
    const view = catchUpView({
      liveServes: false,
      live: live({}),
      joinedAt: null,
      audio: audio({ status: 'transcribing', progress: 0.5, etaS: [30, 90] }),
    })
    expect(view).toMatchObject({
      phase: 'preparing',
      step: 'transcribing',
      progress: 0.5,
      etaS: [30, 90],
    })
  })

  it('keeps the previous brief in view while the next one is prepared', () => {
    const view = catchUpView({
      liveServes: false,
      live: live({}),
      joinedAt: null,
      audio: audio({
        status: 'transcribing',
        progress: 0.3,
        overdue: true,
        brief: { text: 'Old', coveredUntil: JOINED },
      }),
    })
    expect(view).toMatchObject({
      phase: 'text',
      text: 'Old',
      coveredUntil: JOINED,
      step: 'transcribing',
      progress: 0.3,
      overdue: true,
    })
  })

  it('says why the brief failed, or keeps the one left as not updated', () => {
    const failed = (brief: AudioCatchUpState['brief'], error?: string) =>
      catchUpView({
        liveServes: false,
        live: live({}),
        joinedAt: null,
        audio: audio({ status: 'failed', brief, error }),
      })
    expect(failed(null, 'summary_service')).toMatchObject({
      phase: 'failed',
      failure: 'service',
      canRetry: true,
    })
    expect(failed(null, 'linto_error')).toMatchObject({ failure: 'service' })
    expect(failed(null)).toMatchObject({ failure: 'generic', canRetry: true })
    expect(failed({ text: 'Old', coveredUntil: 1 }, 'timeout')).toMatchObject({
      phase: 'text',
      text: 'Old',
      stale: true,
      failure: 'timeout',
      canRetry: true,
    })
  })

  it.each([
    ['transcription', 'transcription'],
    ['transcription_gap', 'gap'],
    ['stopped', 'stopped'],
  ])('offers no retry when %s failed', (error, failure) => {
    expect(
      catchUpView({
        liveServes: false,
        live: live({}),
        joinedAt: null,
        audio: audio({ status: 'failed', error }),
      })
    ).toMatchObject({ failure, canRetry: false })
  })

  it('flags a ready brief the server could not update', () => {
    const view = catchUpView({
      liveServes: false,
      live: live({}),
      joinedAt: null,
      audio: audio({
        status: 'ready',
        brief: { text: 'Old', coveredUntil: 1 },
        stale: true,
        error: 'transcription_gap',
      }),
    })
    expect(view).toMatchObject({
      phase: 'text',
      stale: true,
      failure: 'gap',
      canRetry: false,
    })
  })

  it('says the connection is lost while the polls keep trying', () => {
    expect(
      catchUpView({
        liveServes: false,
        live: live({}),
        joinedAt: null,
        audio: audio({ status: 'transcribing', reconnecting: true }),
      })
    ).toMatchObject({ phase: 'preparing', reconnecting: true })
  })

  it('says why the live summary failed, keeping what streamed', () => {
    const view = (state: Partial<LintoCatchUpState>) =>
      catchUpView({
        liveServes: true,
        live: live({ status: 'error', ...state }),
        joinedAt: JOINED,
        audio: audio({}),
      })
    expect(view({ error: 'catchup_unavailable' })).toMatchObject({
      phase: 'failed',
      failure: 'service',
      canRetry: true,
    })
    expect(view({ error: 'catchup_error' })).toMatchObject({
      phase: 'failed',
      failure: 'generic',
    })
    expect(view({ text: 'Le début ' })).toMatchObject({
      phase: 'text',
      text: 'Le début',
      stale: true,
      failure: 'interrupted',
      canRetry: true,
    })
  })

  it('waits for the first words of the live summary', () => {
    expect(
      catchUpView({
        liveServes: true,
        live: live({ status: 'loading' }),
        joinedAt: JOINED,
        audio: audio({}),
      })
    ).toMatchObject({ phase: 'preparing', step: null, progress: null })
    expect(
      catchUpView({
        liveServes: true,
        live: live({ status: 'streaming', text: 'En c' }),
        joinedAt: JOINED,
        audio: audio({}),
      })
    ).toMatchObject({ phase: 'text', text: 'En c' })
  })

  it('says when too little was said', () => {
    expect(
      catchUpView({
        liveServes: false,
        live: live({}),
        joinedAt: null,
        audio: audio({ status: 'too_short' }),
      })?.phase
    ).toBe('too_short')
  })

  it.each([
    ['idle', live({}), audio({})],
    ['live unavailable', live({ status: 'unavailable' }), audio({})],
    ['empty live result', live({ status: 'done', text: '  ' }), audio({})],
    ['closed', live({ status: 'done', text: 'x', dismissed: true }), audio({})],
  ])('shows nothing for the live source when %s', (_label, liveState, a) => {
    expect(
      catchUpView({ liveServes: true, live: liveState, joinedAt: 1, audio: a })
    ).toBeNull()
  })

  it.each([
    ['idle', audio({})],
    ['unavailable', audio({ status: 'unavailable' })],
    ['ready without brief', audio({ status: 'ready' })],
    [
      'closed',
      audio({
        status: 'ready',
        brief: { text: 'x', coveredUntil: 1 },
        dismissed: true,
      }),
    ],
  ])('shows nothing for the audio source when %s', (_label, a) => {
    expect(
      catchUpView({ liveServes: false, live: live({}), joinedAt: 1, audio: a })
    ).toBeNull()
  })
})

describe('isLateArrival', () => {
  const start = Date.parse('2026-11-20T10:00:00Z')

  it('is late only past the threshold after the start', () => {
    expect(isLateArrival(start + 60_000, start, 60)).toBe(false)
    expect(isLateArrival(start + 61_000, start, 60)).toBe(true)
    expect(isLateArrival(start - 5_000, start, 60)).toBe(false)
  })

  it('is never late without a start', () => {
    expect(isLateArrival(start, Number.NaN, 60)).toBe(false)
  })
})
