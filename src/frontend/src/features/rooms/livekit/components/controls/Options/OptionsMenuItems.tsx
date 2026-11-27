import { Menu as RACMenu, MenuSection } from 'react-aria-components'
import { Separator } from '@/primitives/Separator'
import { FullScreenMenuItem } from './FullScreenMenuItem'
import { SettingsMenuItem } from './SettingsMenuItem'
import { FeedbackMenuItem } from './FeedbackMenuItem'
import { EffectsMenuItem } from './EffectsMenuItem'
import { SupportMenuItem } from './SupportMenuItem'
import { DocumentationMenuItem } from './DocumentationMenuItem'
import { TranscriptMenuItem } from './TranscriptMenuItem'
import { ScreenRecordingMenuItem } from './ScreenRecordingMenuItem'
import { isIntentPage } from '@/features/intent/isIntentPage'
import { PictureInPictureMenuItem } from '@/features/rooms/livekit/components/controls/Options/PictureInPictureMenuItem'

// @todo try refactoring it to use MenuList component
export const OptionsMenuItems = () => {
  // PiP, transcript and recording are Meet features not planned in the /intents frame
  const isIntent = isIntentPage()
  return (
    <RACMenu
      style={{
        minWidth: '150px',
        width: '300px',
      }}
    >
      <MenuSection>
        {!isIntent && <PictureInPictureMenuItem />}
        {!isIntent && <TranscriptMenuItem />}
        {!isIntent && <ScreenRecordingMenuItem />}
        <FullScreenMenuItem />
        <EffectsMenuItem />
      </MenuSection>
      <Separator />
      <MenuSection>
        <SupportMenuItem />
        <DocumentationMenuItem />
        <FeedbackMenuItem />
        <SettingsMenuItem />
      </MenuSection>
    </RACMenu>
  )
}
