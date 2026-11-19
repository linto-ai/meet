import { useEffect } from 'react'

/**
 * Where the sign-in popup of /intents lands: tells the page that opened it,
 * on our own origin only, then closes itself. Shows nothing.
 */
const IntentSignedIn = () => {
  useEffect(() => {
    window.opener?.postMessage(
      { source: 'twake-meet-intent', type: 'signed-in' },
      window.location.origin
    )
    window.close()
  }, [])
  return null
}

export default IntentSignedIn
