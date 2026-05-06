import { getEnv } from '@/utils/getEnv'

export const apiUrl = (path: string, apiVersion = '1.0') => {
  const origin =
    getEnv('VITE_API_BASE_URL') ||
    (typeof window !== 'undefined' ? window.location.origin : '')

  // Remove leading/trailing slashes from origin/path if it exists
  const sanitizedOrigin = origin.replace(/\/$/, '')
  const sanitizedPath = path.replace(/^\//, '')

  return `${sanitizedOrigin}/api/v${apiVersion}/${sanitizedPath}`
}
