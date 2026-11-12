import { isMobileBrowser } from '@livekit/components-core'
import { useSyncExternalStore } from 'react'

// Up to this width (px) the layout is the compact one: a phone, or a narrow
// frame such as the 420 px detached call window of /embed. 600 px is where
// the full control bar (about 570 px) no longer fits in a frame.
const COMPACT_MAX_WIDTH = 600

const subscribe = (onChange: () => void) => {
  window.addEventListener('resize', onChange)
  return () => window.removeEventListener('resize', onChange)
}

const getSnapshot = () => window.innerWidth <= COMPACT_MAX_WIDTH

/**
 * True on a mobile browser or when the viewport is narrow (in /embed the
 * viewport is the iframe). Follows resizes with useSyncExternalStore.
 * Used by: Conference, ControlBar, ReactionButtonsContainer.
 */
export const useIsMobile = () => {
  const isNarrow = useSyncExternalStore(subscribe, getSnapshot)
  return isNarrow || isMobileBrowser()
}
