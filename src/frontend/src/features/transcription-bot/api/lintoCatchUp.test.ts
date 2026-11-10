import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchLintoCatchUpStatus,
  LintoCatchUpError,
  streamLintoCatchUp,
} from './lintoBotApi'

const ROOM = 'room-1'
const TOKEN = 'lk-token'

const sse = (frames: string[], status = 200) => {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  })
  return new Response(body, {
    status,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

const token = (content: string) =>
  `event: token\ndata: ${JSON.stringify({ content })}\n\n`

const stream = (onToken = vi.fn()) =>
  streamLintoCatchUp({
    roomId: ROOM,
    token: TOKEN,
    before: '2026-11-10T10:00:00.000Z',
    onToken,
  })

const failure = async (promise: Promise<unknown>) => {
  try {
    await promise
  } catch (err) {
    return err as LintoCatchUpError
  }
  throw new Error('expected a rejection')
}

beforeEach(() => {
  vi.stubGlobal('document', { cookie: 'csrftoken=csrf-1' })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('streamLintoCatchUp', () => {
  it('posts the join time to the Meet relay with the LiveKit token', async () => {
    const fetchMock = vi.fn(async () => sse([token('ok')]))
    vi.stubGlobal('fetch', fetchMock)
    await stream()

    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ]
    expect(url).toBe(`/api/v1.0/rooms/${ROOM}/linto/catchup/`)
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('include')
    expect(init.headers).toMatchObject({
      Authorization: `X-LiveKit-Token ${TOKEN}`,
      'X-CSRFToken': 'csrf-1',
    })
    expect(JSON.parse(init.body as string)).toEqual({
      before: '2026-11-10T10:00:00.000Z',
    })
  })

  it('streams the tokens, even split across chunks, and reads done', async () => {
    const frames = [
      token('Voici '),
      'event: token\ndata: {"content":"le ',
      'résumé."}\n\n',
      `event: done\ndata: ${JSON.stringify({ cached: true })}\n\n`,
    ]
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => sse(frames))
    )
    const onToken = vi.fn()
    const result = await stream(onToken)

    expect(result).toEqual({ text: 'Voici le résumé.', cached: true })
    expect(onToken.mock.calls).toEqual([
      ['Voici ', 'Voici '],
      ['le résumé.', 'Voici le résumé.'],
    ])
  })

  it('rejects on an error event with its message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sse([
          token('Voici '),
          `event: error\ndata: ${JSON.stringify({ message: 'LLM stream timed out' })}\n\n`,
        ])
      )
    )
    const err = await failure(stream())
    expect(err).toBeInstanceOf(LintoCatchUpError)
    expect(err.code).toBe('catchup_error')
    expect(err.message).toBe('LLM stream timed out')
  })

  it('a 204 means too little was said', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(null, { status: 204 }))
    )
    const err = await failure(stream())
    expect(err.code).toBe('catchup_too_short')
    expect(err.status).toBe(204)
  })

  it("takes the code and the reason from the backend's answer", async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json(503, {
          code: 'catchup_unavailable',
          reason: 'Studio has no catch-up route',
        })
      )
    )
    const err = await failure(stream())
    expect(err.code).toBe('catchup_unavailable')
    expect(err.status).toBe(503)
    expect(err.reason).toBe('Studio has no catch-up route')
  })

  it.each([
    [429, 'catchup_rate_limited'],
    [503, 'catchup_unavailable'],
    [403, 'catchup_forbidden'],
    [401, 'catchup_unauthorized'],
    [404, 'catchup_error'],
    [500, 'catchup_error'],
  ])('a %i without a code is %s', async (status, code) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json(status, { detail: 'nope' }))
    )
    const err = await failure(stream())
    expect(err.code).toBe(code)
    expect(err.status).toBe(status)
  })
})

describe('fetchLintoCatchUpStatus', () => {
  it('reads the availability and the reason', async () => {
    const fetchMock = vi.fn(async () =>
      json(200, { enabled: false, reason: 'catch-up off for this run' })
    )
    vi.stubGlobal('fetch', fetchMock)
    expect(await fetchLintoCatchUpStatus(ROOM, TOKEN)).toEqual({
      enabled: false,
      reason: 'catch-up off for this run',
    })
    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ]
    expect(url).toBe(`/api/v1.0/rooms/${ROOM}/linto/catchup/status/`)
    expect((init.headers as Record<string, string>).Authorization).toBe(
      `X-LiveKit-Token ${TOKEN}`
    )
  })

  it('turns an HTTP failure into a catch-up error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json(404, { detail: 'Not found.' }))
    )
    const err = await failure(fetchLintoCatchUpStatus(ROOM, TOKEN))
    expect(err).toBeInstanceOf(LintoCatchUpError)
    expect(err.status).toBe(404)
    expect(err.code).toBe('catchup_error')
  })
})
