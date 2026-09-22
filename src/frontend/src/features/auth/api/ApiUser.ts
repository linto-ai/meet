import { BackendLanguage } from '@/utils/languages'
import type {
  ApiAccessLevel,
  RoomConfiguration,
} from '@/features/rooms/api/ApiRoom'
import type { LintoCapabilities } from '@/features/transcription-bot/types/linto'

export type ApiUser = {
  id: string
  email: string
  full_name: string
  last_name: string
  language: BackendLanguage
  timezone: string
  default_room_access_level?: ApiAccessLevel | null
  default_room_configuration?: RoomConfiguration | null
  can_create?: boolean
  // LinTO capabilities (fork): null/absent = no AI button at all.
  linto?: LintoCapabilities | null
}
