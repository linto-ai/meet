import type { Track } from 'livekit-client'
import { RecordingPermission } from '@/features/recording/types'
type Source = Track.Source

export type ApiLiveKit = {
  url: string
  room: string
  token: string
}

export enum ApiAccessLevel {
  PUBLIC = 'public',
  TRUSTED = 'trusted',
  RESTRICTED = 'restricted',
}

export type RoomConfiguration = {
  can_publish_sources?: Source[] | null
  everyone_can_mute?: boolean | null
  screen_recording_permission?: RecordingPermission
  transcript_permission?: RecordingPermission
}

export type ApiRoom = {
  id: string
  name: string
  slug: string
  pin_code?: string
  is_administrable: boolean
  access_level: ApiAccessLevel
  livekit?: ApiLiveKit
  configuration?: RoomConfiguration
  recording_permissions?: {
    screen_recording_permission?: RecordingPermission
    transcript_permission?: RecordingPermission
  }
}
