/// <reference types="vite/client" />

// Version of @mediapipe/tasks-vision, injected at build time (vite.config.ts).
declare const __MEDIAPIPE_VERSION__: string

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
  readonly VITE_APP_TITLE: string
  readonly VITE_BRIDGE_TARGET_ORIGIN_ALLOWLIST?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

interface Window {
  VITE_CONFIG?: Partial<Record<keyof ImportMetaEnv, string>>
}
