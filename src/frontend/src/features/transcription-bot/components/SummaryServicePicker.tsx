import { useEffect, useMemo } from 'react'
import { RadioGroup } from 'react-aria-components'
import { useTranslation } from 'react-i18next'
import { useSnapshot } from 'valtio'
import {
  RiArticleLine,
  RiChat3Line,
  RiFileListLine,
  RiFileTextLine,
  RiListCheck2,
  RiSparklingLine,
  RiStickyNoteLine,
  RiTodoLine,
} from '@remixicon/react'
import { css } from '@/styled-system/css'
import { Text } from '@/primitives'
import { Radio } from '@/primitives/Radio'
import { useRoomData } from '@/features/rooms/livekit/hooks/useRoomData'
import { lintoStore } from '../store/lintoStore'
import { useLintoSummaryServices } from '../api/lintoBotApi'
import type { LintoSummaryService } from '../types/linto'

// The gateway names icons with the Phosphor vocabulary (as its document
// templates do); Meet draws with Remix. Unknown or absent → a generic spark.
const ICONS: Record<string, typeof RiSparklingLine> = {
  'file-text': RiFileTextLine,
  article: RiArticleLine,
  'list-checks': RiListCheck2,
  'list-bullets': RiFileListLine,
  'check-square': RiTodoLine,
  note: RiStickyNoteLine,
  'note-pencil': RiStickyNoteLine,
  chats: RiChat3Line,
  'chat-text': RiChat3Line,
  sparkle: RiSparklingLine,
}

const summaryServiceIcon = (icon: string | null | undefined) =>
  (icon && ICONS[icon]) || RiSparklingLine

const localizedDescription = (
  description: Record<string, string> | undefined,
  locale: string
) => {
  if (!description) return ''
  const base = locale.split('-')[0]
  return description[locale] ?? description[base] ?? description.en ?? ''
}

interface SummaryServicePickerProps {
  isDisabled?: boolean
  // Controlled use (the deferred-transcription tool keeps its own choice);
  // without these, the picker reads and writes the live panel's store.
  value?: string
  onChange?: (route: string) => void
}

/**
 * Which summary the meeting gets: the LLM Gateway services carrying the
 * `meet` scope, with their icon. The choice travels with `linto/started`
 * (live) or in the recording options (deferred) and reaches Studio when the
 * summary is triggered. Nothing is rendered when the backend offers no
 * service (the instance default applies). Both transcription tools use it.
 */
export const SummaryServicePicker = ({
  isDisabled,
  value,
  onChange,
}: SummaryServicePickerProps) => {
  const { t, i18n } = useTranslation('transcription-bot', {
    keyPrefix: 'lintoBot.options',
  })
  const { summaryService: storeService } = useSnapshot(lintoStore)
  const controlled = onChange !== undefined
  const summaryService = controlled ? value : storeService
  const setService = (route: string) => {
    if (controlled) onChange(route)
    else lintoStore.summaryService = route
  }
  const apiRoomData = useRoomData()
  const { data } = useLintoSummaryServices(
    apiRoomData?.livekit?.room,
    apiRoomData?.livekit?.token
  )
  const services = useMemo(() => data ?? [], [data])

  // Default to the instance's service (or the first) once the list arrives;
  // keep a previous choice as long as it is still offered.
  useEffect(() => {
    if (services.length === 0) return
    const current = controlled ? value : lintoStore.summaryService
    if (current && services.some((s) => s.route === current)) return
    const preferred = services.find((s) => s.default) ?? services[0]
    if (controlled) onChange(preferred.route)
    else lintoStore.summaryService = preferred.route
    // `onChange` is expected stable enough; a new identity must not re-default.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [services, controlled, value])

  if (services.length === 0) return null

  return (
    <div
      data-testid="linto-summary-services"
      className={css({ width: '100%', paddingLeft: '1.625rem' })}
    >
      <RadioGroup
        aria-label={t('summaryService')}
        value={summaryService ?? ''}
        isDisabled={isDisabled}
        onChange={setService}
        className={css({
          display: 'flex',
          flexDirection: 'column',
          gap: '0.375rem',
          marginTop: '0.375rem',
        })}
      >
        <Text variant="sm">{t('summaryService')}</Text>
        {services.map((service: LintoSummaryService) => {
          const Icon = summaryServiceIcon(service.icon)
          const description = localizedDescription(
            service.description,
            i18n.language
          )
          return (
            <Radio
              key={service.route}
              value={service.route}
              data-testid={`linto-summary-service-${service.route}`}
            >
              <span
                className={css({
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                  minWidth: 0,
                })}
              >
                <Icon size={18} aria-hidden="true" />
                <span
                  className={css({
                    display: 'flex',
                    flexDirection: 'column',
                    minWidth: 0,
                  })}
                >
                  <Text variant="sm">{service.name}</Text>
                  {description && <Text variant="xsNote">{description}</Text>}
                </span>
              </span>
            </Radio>
          )
        })}
      </RadioGroup>
    </div>
  )
}
