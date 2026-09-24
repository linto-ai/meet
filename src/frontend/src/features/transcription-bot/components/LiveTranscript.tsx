import { css } from '@/styled-system/css'
import { RiCloseLine } from '@remixicon/react'
import { Button, Text } from '@/primitives'
import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import { transcriptStore } from '../store/transcriptStore'
import { lintoStore } from '../store/lintoStore'
import { dismissLintoCatchUp } from '../hooks/useLintoCatchUp'
import { LintoCaption } from '../types/linto'
import { baseCode, languageName, ORIGINAL } from '../utils/languageLabel'
import { useLintoDisplayLanguages } from '../hooks/useLintoDisplayLanguages'
import { SimpleMarkdown } from './SimpleMarkdown'

// Find the translation for a (base) language inside an entry, tolerant of region
// tags on the stored keys.
const translationFor = (
  translations: Record<string, string> | undefined,
  lang: string
): string | undefined => {
  if (!translations) return undefined
  if (translations[lang] != null) return translations[lang]
  const hit = Object.entries(translations).find(([k]) => baseCode(k) === lang)
  return hit?.[1]
}

// Wall-clock HH:MM of when the utterance was received (≈ when it was said).
const formatTime = (ms: number, locale: string): string => {
  if (!ms) return ''
  try {
    return new Date(ms).toLocaleTimeString(locale, {
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
}

// How far from the bottom (px) still counts as "reading the live tail".
const STICK_THRESHOLD_PX = 32
// A scroll event this soon after a wheel / touch / key gesture is the user's.
const USER_GESTURE_WINDOW_MS = 400
// Keys that scroll a focused container upward (the only way to leave the tail).
const SCROLL_UP_KEYS = new Set(['ArrowUp', 'PageUp', 'Home'])

const selectClass = css({
  width: '100%',
  padding: '0.4rem',
  borderRadius: '4px',
  border: '1px solid',
  borderColor: 'control.border',
  backgroundColor: 'white',
})

/**
 * Live transcript as a meeting LOG: one entry per caption (each final is its own
 * timestamped line, never concatenated with the previous one). The speaker name
 * heads a run of consecutive same-speaker entries; the in-flight partial shows
 * dimmed at the tail and refreshes in place.
 */
export const LiveTranscript = () => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'lintoBot',
  })
  const { startedByMe, joinedAt, catchUp } = useSnapshot(lintoStore)
  const { byId, order } = useSnapshot(transcriptStore)
  const {
    languages: availableLangs,
    effective: effectiveDisplay,
    setDisplay,
  } = useLintoDisplayLanguages()

  // ── The transcript zone scrolls on its own and follows the live tail ──
  // While the reader sits at the bottom, every change (new line, partial
  // refresh, translation, history hydration) keeps the tail in view; only a
  // USER gesture scrolling up (wheel, touch, keyboard, scrollbar drag) leaves
  // it. The view then stays put and a pill counts the lines that arrived
  // meanwhile — clicking it (or scrolling back down) re-attaches to the tail.
  // Programmatic or layout-driven scroll events never detach: a smooth
  // animation, a shrinking partial or an insertion above would otherwise
  // unhook the follow mode behind the reader's back.
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const contentRef = useRef<HTMLDivElement | null>(null)
  const stickRef = useRef(true)
  const lastGestureRef = useRef(0)
  const draggingRef = useRef(false)
  const lastScrollTopRef = useRef(0)
  const [detached, setDetached] = useState(false)
  const [unseen, setUnseen] = useState(0)
  const seenIdsRef = useRef<Set<string>>(new Set())
  // A line flashes once when it becomes FINAL: the partial shows dimmed and
  // quiet, the flash marks the moment the wording settles. Lines already final
  // when the journal mounts (reopened panel, hydrated history) stay quiet.
  const [flashIds, setFlashIds] = useState<Set<string>>(() => new Set())
  const partialStateRef = useRef<Map<string, boolean>>(new Map())
  const journalReadyRef = useRef(false)

  const isAtBottom = (el: HTMLDivElement) =>
    el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX

  const scrollToBottom = useCallback((smooth: boolean) => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const movingDown = el.scrollTop >= lastScrollTopRef.current
    lastScrollTopRef.current = el.scrollTop
    if (isAtBottom(el)) {
      // Back at the tail (the reader scrolled down, or the pill's animation
      // landed): follow again.
      if (!stickRef.current && movingDown) {
        stickRef.current = true
        setDetached(false)
        setUnseen(0)
      }
      return
    }
    const byUser =
      draggingRef.current ||
      Date.now() - lastGestureRef.current < USER_GESTURE_WINDOW_MS
    if (byUser && !movingDown && stickRef.current) {
      stickRef.current = false
      setDetached(true)
    }
  }

  const reattach = () => {
    stickRef.current = true
    setDetached(false)
    setUnseen(0)
    scrollToBottom(true)
  }

  // One entry per caption, in arrival order (partials refresh their own id).
  const entries = useMemo(
    () =>
      order
        .map((id) => byId[id])
        .filter((c): c is LintoCaption => !!c && !!c.text.trim()),
    [byId, order]
  )

  // Catch-up: everything said BEFORE I joined is history, shown dimmed above a
  // "you joined at HH:MM" divider. The starter saw it all and gets neither.
  // The summary block only shows while there is (or will be) something to
  // read: nothing at all when the run has no summary, when too little was
  // said, when the LLM failed, or once the reader closed it.
  const showCatchUp =
    !startedByMe &&
    joinedAt !== null &&
    !catchUp.dismissed &&
    (catchUp.status === 'loading' ||
      catchUp.status === 'streaming' ||
      (catchUp.status === 'done' && !!catchUp.text.trim()))
  // The divider sits right before the first line received live (or at the tail
  // when the whole journal is still history).
  const markerIndex = useMemo(() => {
    if (startedByMe || joinedAt === null) return -1
    if (!entries.some((entry) => entry.catchup)) return -1
    const firstLive = entries.findIndex((entry) => !entry.catchup)
    return firstLive === -1 ? entries.length : firstLive
  }, [entries, startedByMe, joinedAt])

  const joinedMarker =
    markerIndex === -1 ? null : (
      <div
        key="linto-joined-marker"
        data-testid="linto-joined-marker"
        className={css({
          display: 'flex',
          alignItems: 'center',
          gap: '0.5rem',
          marginTop: '0.75rem',
          marginBottom: '0.25rem',
          '&::before, &::after': {
            content: '""',
            flex: 1,
            height: '1px',
            backgroundColor: 'greyscale.200',
          },
        })}
      >
        <Text
          variant="smNote"
          as="span"
          className={css({ flexShrink: 0, textStyle: 'sm' })}
        >
          {t('catchup.joined', {
            time: formatTime(joinedAt ?? 0, i18n.language),
          })}
        </Text>
      </div>
    )

  useEffect(() => {
    const next: string[] = []
    for (const entry of entries) {
      const wasPartial = partialStateRef.current.get(entry.id)
      const becameFinal = wasPartial === true && !entry.partial
      const arrivedFinal =
        wasPartial === undefined && !entry.partial && journalReadyRef.current
      if (!entry.catchup && (becameFinal || arrivedFinal)) next.push(entry.id)
      partialStateRef.current.set(entry.id, entry.partial)
    }
    journalReadyRef.current = true
    if (next.length > 0) {
      setFlashIds((current) => new Set([...current, ...next]))
    }
  }, [entries])

  // Before paint: stay on the tail while attached; otherwise count the NEW
  // live lines for the pill (hydrated history inserted above is not "new").
  useLayoutEffect(() => {
    const previous = seenIdsRef.current
    const current = new Set<string>()
    let added = 0
    for (const entry of entries) {
      current.add(entry.id)
      if (!previous.has(entry.id) && !entry.catchup) added += 1
    }
    seenIdsRef.current = current
    if (stickRef.current) scrollToBottom(false)
    else if (added > 0) setUnseen((n) => n + added)
  }, [entries, effectiveDisplay, scrollToBottom])

  const hasEntries = entries.length > 0

  // User gestures that scroll UP (wheel, touch, keyboard, scrollbar grab) mark
  // the next scroll events as the reader's own; only those may detach.
  useEffect(() => {
    if (!hasEntries) return
    const el = scrollRef.current
    if (!el) return
    let touchY: number | null = null
    const markGesture = () => {
      lastGestureRef.current = Date.now()
    }
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) markGesture()
    }
    const onTouchStart = (e: TouchEvent) => {
      touchY = e.touches[0]?.clientY ?? null
    }
    const onTouchMove = (e: TouchEvent) => {
      const y = e.touches[0]?.clientY
      // Finger moving DOWN scrolls the content up (towards older lines).
      if (y != null && touchY != null && y > touchY) markGesture()
      touchY = y ?? null
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (SCROLL_UP_KEYS.has(e.key) || (e.key === ' ' && e.shiftKey)) {
        markGesture()
      }
    }
    // A press on the container itself (not a line) is a scrollbar grab: its
    // scroll events count as the reader's until the pointer is released.
    const onPointerDown = (e: PointerEvent) => {
      if (e.target === el) draggingRef.current = true
    }
    const release = () => {
      draggingRef.current = false
    }
    const passive = { passive: true }
    el.addEventListener('wheel', onWheel, passive)
    el.addEventListener('touchstart', onTouchStart, passive)
    el.addEventListener('touchmove', onTouchMove, passive)
    el.addEventListener('keydown', onKeyDown)
    el.addEventListener('pointerdown', onPointerDown)
    window.addEventListener('pointerup', release)
    window.addEventListener('pointercancel', release)
    return () => {
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('keydown', onKeyDown)
      el.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('pointerup', release)
      window.removeEventListener('pointercancel', release)
      draggingRef.current = false
    }
  }, [hasEntries])

  // Height changes that bring no new entry (a partial shrinking, a translation
  // landing, the "finalized" animation, the catch-up block resizing the
  // viewport) re-pin the tail while attached.
  useEffect(() => {
    if (!hasEntries) return
    const el = scrollRef.current
    const content = contentRef.current
    if (!el || !content || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (stickRef.current) scrollToBottom(false)
    })
    observer.observe(content)
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasEntries, scrollToBottom])

  return (
    <div
      data-testid="linto-live-transcript"
      className={css({
        width: '100%',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.75rem',
        marginBottom: '1rem',
        flex: 1,
        minHeight: 0,
      })}
    >
      <Text variant="h3" margin={false}>
        {t('live.heading')}
      </Text>

      {availableLangs.length > 0 && (
        <label className={css({ width: '100%' })}>
          <Text variant="sm">{t('live.displayLanguage')}</Text>
          <select
            data-testid="linto-display-language"
            className={selectClass}
            value={effectiveDisplay}
            onChange={(e) => setDisplay(e.target.value)}
          >
            <option value={ORIGINAL}>{t('live.original')}</option>
            {availableLangs.map((lang) => (
              <option key={lang} value={lang}>
                {languageName(lang, i18n.language)}
              </option>
            ))}
          </select>
        </label>
      )}

      {showCatchUp && (
        <div
          data-testid="linto-catchup"
          data-status={catchUp.status}
          className={css({
            width: '100%',
            padding: '0.625rem 0.75rem',
            borderRadius: '4px',
            border: '1px solid',
            borderColor: 'control.border',
            backgroundColor: 'greyscale.50',
            flexShrink: 0,
            maxHeight: '40%',
            overflowY: 'auto',
          })}
        >
          <div
            className={css({
              display: 'flex',
              alignItems: 'baseline',
              justifyContent: 'space-between',
              gap: '0.5rem',
            })}
          >
            <Text
              variant="sm"
              as="p"
              className={css({ fontWeight: 'semibold', color: 'primary.700' })}
            >
              {t('catchup.heading')}
            </Text>
            <Button
              variant="text"
              size="sm"
              data-testid="linto-catchup-close"
              aria-label={t('catchup.close')}
              onPress={() => dismissLintoCatchUp()}
            >
              <RiCloseLine size={16} aria-hidden="true" />
            </Button>
          </div>
          <div data-testid="linto-catchup-summary">
            {catchUp.text ? (
              <SimpleMarkdown text={catchUp.text} />
            ) : (
              <Text variant="smNote">{t('catchup.loading')}</Text>
            )}
          </div>
        </div>
      )}

      {entries.length === 0 ? (
        <Text variant="smNote">{t('live.empty')}</Text>
      ) : (
        <div
          className={css({
            position: 'relative',
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
          })}
        >
          <div
            ref={scrollRef}
            onScroll={onScroll}
            data-testid="linto-journal"
            data-detached={detached ? 'true' : 'false'}
            className={css({
              flex: 1,
              minHeight: 0,
              overflowY: 'auto',
              overscrollBehavior: 'contain',
              // Pinning is done by hand; the browser's own anchoring would
              // fight it when history is inserted above.
              overflowAnchor: 'none',
              paddingRight: '0.25rem',
            })}
          >
            <div
              ref={contentRef}
              className={css({
                display: 'flex',
                flexDirection: 'column',
                gap: '0.125rem',
              })}
            >
              {entries.map((entry, index) => {
                const showSpeaker =
                  index === 0 || entries[index - 1].locutor !== entry.locutor
                const showTranslated = effectiveDisplay !== ORIGINAL
                const translated = translationFor(
                  entry.translations,
                  effectiveDisplay
                )
                // When the chosen language hasn't arrived for THIS line yet, fall
                // back to the original so it never blanks out mid-stream.
                const body = showTranslated
                  ? (translated ?? entry.text)
                  : entry.text
                const isTranslated = showTranslated && translated != null
                const time = formatTime(entry.receivedAt, i18n.language)
                // The animation starts when the id enters the set, i.e. the
                // instant the line became final; it never restarts afterwards.
                const isFresh = !entry.partial && flashIds.has(entry.id)
                return (
                  <Fragment key={entry.id}>
                    {index === markerIndex && joinedMarker}
                    <div
                      data-testid="linto-turn"
                      data-speaker={entry.locutor}
                      data-partial={entry.partial ? 'true' : 'false'}
                      data-fresh={isFresh ? 'true' : 'false'}
                      {...(entry.catchup ? { 'data-catchup': 'true' } : {})}
                      className={css({
                        width: '100%',
                        marginTop: showSpeaker ? '0.5rem' : 0,
                        opacity: entry.catchup ? 0.75 : 1,
                        borderRadius: '4px',
                        marginLeft: '-0.25rem',
                        paddingLeft: '0.25rem',
                        animation: isFresh
                          ? 'linto_new_line 1.8s ease-out'
                          : undefined,
                      })}
                    >
                      {showSpeaker && (
                        <Text
                          variant="sm"
                          className={css({
                            fontWeight: 'semibold',
                            color: 'primary.700',
                          })}
                        >
                          {entry.locutor}
                        </Text>
                      )}
                      <div
                        className={css({
                          display: 'flex',
                          gap: '0.5rem',
                          alignItems: 'baseline',
                          opacity: entry.partial ? 0.6 : 1,
                          fontStyle: entry.partial ? 'italic' : 'normal',
                        })}
                      >
                        {time && (
                          <span
                            data-testid="linto-turn-time"
                            className={css({
                              flexShrink: 0,
                              fontVariantNumeric: 'tabular-nums',
                              fontSize: '0.6875rem',
                              color: 'greyscale.500',
                              paddingTop: '0.15rem',
                            })}
                          >
                            {time}
                          </span>
                        )}
                        {isTranslated ? (
                          <div
                            data-testid="linto-translation"
                            data-lang={effectiveDisplay}
                          >
                            <Text variant="sm">{body}</Text>
                          </div>
                        ) : (
                          <Text variant="sm">{body}</Text>
                        )}
                      </div>
                    </div>
                  </Fragment>
                )
              })}
              {markerIndex === entries.length && joinedMarker}
            </div>
          </div>
          {detached && unseen > 0 && (
            <button
              type="button"
              data-testid="linto-journal-new-lines"
              onClick={reattach}
              className={css({
                position: 'absolute',
                bottom: '0.5rem',
                left: '50%',
                transform: 'translateX(-50%)',
                paddingY: '0.25rem',
                paddingX: '0.75rem',
                borderRadius: '999px',
                fontSize: '0.8125rem',
                backgroundColor: 'primary',
                color: 'white',
                boxShadow: '0 2px 8px rgba(0,0,0,0.25)',
                cursor: 'pointer',
                animation: 'fade 0.2s ease-out',
              })}
            >
              {t('live.newLines', { count: unseen })} ↓
            </button>
          )}
        </div>
      )}
    </div>
  )
}
