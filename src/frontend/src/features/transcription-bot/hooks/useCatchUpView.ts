import { useEffect } from 'react'
import { useSnapshot } from 'valtio'
import { closeSidePanel, layoutStore } from '@/stores/layout'
import { PanelId } from '@/features/rooms/livekit/hooks/useSidePanel'
import {
  audioCatchUpStore,
  dismissAudioCatchUp,
} from '../store/audioCatchUpStore'
import { lintoStore } from '../store/lintoStore'
import { catchUpView, type CatchUpView } from '../utils/catchUpView'
import { dismissLintoCatchUp } from './useLintoCatchUp'
import { useLiveServesCatchUp } from './useLiveServesCatchUp'

// "Too little was said" shows this long, then the brief goes.
const TOO_SHORT_MS = 8000

/** The late joiner's brief to show, from a live run or the recording. */
export const useCatchUpView = (): CatchUpView | null => {
  const liveServes = useLiveServesCatchUp()
  const { catchUp: live, joinedAt } = useSnapshot(lintoStore)
  const { catchUp: audio } = useSnapshot(audioCatchUpStore)
  return catchUpView({ liveServes, live, joinedAt, audio })
}

/** Drops a "too little was said" brief after a while, and its panel if open. */
export const useCatchUpTooShortDismiss = () => {
  const view = useCatchUpView()
  const tooShort = view?.phase === 'too_short'
  const source = view?.source
  useEffect(() => {
    if (!tooShort) return
    const timer = window.setTimeout(() => {
      if (source === 'live') dismissLintoCatchUp()
      else dismissAudioCatchUp()
      if (layoutStore.activePanelId === PanelId.CATCH_UP) closeSidePanel()
    }, TOO_SHORT_MS)
    return () => window.clearTimeout(timer)
  }, [tooShort, source])
}
