import { useSnapshot } from 'valtio'
import { useTranscriptionLanguage } from '@/features/settings'
import { useLintoConfig } from '@/features/transcription-bot'
import { LINTO_LANGUAGE_AUTO, recordingStore } from '@/stores/recording'

/**
 * The language a recording is transcribed in, as sent in the recording
 * options (`undefined` = detected by the service). When LinTO serves the
 * transcription it is the deferred tool's ASR language (automatic by
 * default), shared by both recording tools; the upstream setting (French by
 * default) only applies without LinTO.
 */
export const useRecordingLanguage = (): string | undefined => {
  const { enabled: isLintoEnabled } = useLintoConfig()
  const { lintoLanguage } = useSnapshot(recordingStore)
  const { selectedLanguageKey, isLanguageSetToAuto } =
    useTranscriptionLanguage()

  if (isLintoEnabled) {
    return lintoLanguage !== LINTO_LANGUAGE_AUTO ? lintoLanguage : undefined
  }
  return isLanguageSetToAuto ? undefined : selectedLanguageKey
}
