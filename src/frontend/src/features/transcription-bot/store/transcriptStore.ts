import { proxy } from 'valtio'
import { LINTO_SEGMENT_PREFIX, LintoCaption } from '../types/linto'

// Keep the transcript bounded: a long meeting can produce thousands of lines.
const MAX_LINES = 2000

type TranscriptState = {
  // Caption lines keyed by segment id, plus the insertion order. Valtio proxies
  // both so the panel and the overlay re-render on any upsert.
  byId: Record<string, LintoCaption>
  order: string[]
}

export const transcriptStore = proxy<TranscriptState>({
  byId: {},
  order: [],
})

/**
 * Split a LinTO segment id into its base id and optional translation target.
 *
 * The bot namespaces its ids as `linto:[<channelKey>:]<segmentId>[:<lang>]`,
 * where `channelKey` is the `<sessionId>,<channelIndex>` tail of its Transcriber
 * stream (present since the per-channel namespacing; absent on older bots) and
 * `segmentId` is the Transcriber's integer counter. Only the TRAILING part can be
 * a language code, and it is never numeric, which is what tells
 * `linto:abc,0:12:en` (translation of `linto:abc,0:12`) apart from
 * `linto:abc,0:12` (an original line).
 *   `linto:12`          → { base: 'linto:12' }
 *   `linto:12:en`       → { base: 'linto:12', lang: 'en' }
 *   `linto:s1,0:12`     → { base: 'linto:s1,0:12' }
 *   `linto:s1,0:12:en`  → { base: 'linto:s1,0:12', lang: 'en' }
 * Returns undefined for ids that are not LinTO segments.
 */
export const parseLintoSegmentId = (
  id: string
): { base: string; lang?: string } | undefined => {
  if (!id.startsWith(LINTO_SEGMENT_PREFIX)) return undefined
  const parts = id.slice(LINTO_SEGMENT_PREFIX.length).split(':')
  const last = parts[parts.length - 1]
  if (parts.length < 2 || last === '' || /^\d+$/.test(last)) {
    return { base: id }
  }
  return {
    base: `${LINTO_SEGMENT_PREFIX}${parts.slice(0, -1).join(':')}`,
    lang: last,
  }
}

const trim = () => {
  const excess = transcriptStore.order.length - MAX_LINES
  if (excess <= 0) return
  const dropped = transcriptStore.order.splice(0, excess)
  for (const id of dropped) delete transcriptStore.byId[id]
}

/**
 * Upsert one ORIGINAL-language line. Partials and the final of the same
 * utterance share an id (Transcriber contract): the text is replaced in place
 * until the final lands, so the line never duplicates.
 */
export const upsertCaption = (caption: LintoCaption) => {
  const existing = transcriptStore.byId[caption.id]
  if (existing) {
    // A final never regresses to a partial (out-of-order delivery).
    if (existing.partial === false && caption.partial) return
    existing.text = caption.text
    existing.partial = caption.partial
    if (caption.language) existing.language = caption.language
    if (caption.locutor && caption.locutor !== existing.locutor) {
      existing.locutor = caption.locutor
    }
    if (caption.participantIdentity) {
      existing.participantIdentity = caption.participantIdentity
    }
    if (caption.startTime !== undefined) existing.startTime = caption.startTime
    if (caption.translations) {
      existing.translations = {
        ...existing.translations,
        ...caption.translations,
      }
    }
    return
  }
  transcriptStore.byId[caption.id] = { ...caption }
  transcriptStore.order.push(caption.id)
  trim()
}

/**
 * Attach a translation to its source line (creating a placeholder line when the
 * translation arrives before the original — the original then fills it).
 */
export const upsertTranslation = (
  baseId: string,
  lang: string,
  text: string,
  receivedAt: number
) => {
  const existing = transcriptStore.byId[baseId]
  if (existing) {
    existing.translations = { ...existing.translations, [lang]: text }
    return
  }
  transcriptStore.byId[baseId] = {
    id: baseId,
    text: '',
    locutor: '',
    partial: true,
    receivedAt,
    translations: { [lang]: text },
  }
  transcriptStore.order.push(baseId)
  trim()
}

/**
 * Hydrate finalized lines fetched from the backend (a participant opening the
 * panel mid-transcription). Lines already known from the live feed win.
 */
export const hydrateCaptions = (captions: LintoCaption[]) => {
  const fresh = captions.filter((c) => c.id && !transcriptStore.byId[c.id])
  if (fresh.length === 0) return
  // Hydrated history precedes anything received live so far.
  const ids: string[] = []
  for (const caption of fresh) {
    transcriptStore.byId[caption.id] = { ...caption }
    ids.push(caption.id)
  }
  transcriptStore.order.unshift(...ids)
  trim()
}

export const clearTranscript = () => {
  transcriptStore.byId = {}
  transcriptStore.order = []
}
