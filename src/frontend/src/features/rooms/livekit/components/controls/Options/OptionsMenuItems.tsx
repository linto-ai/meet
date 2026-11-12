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
import { isEmbedPage } from '@/features/embed/isEmbedPage'
import { PictureInPictureMenuItem } from '@/features/rooms/livekit/components/controls/Options/PictureInPictureMenuItem'

// @todo try refactoring it to use MenuList component
export const OptionsMenuItems = () => {
  // Framed by Chat there is no Meet session: no PiP, transcript or recording
  const isEmbed = isEmbedPage()
  return (
    <RACMenu
      style={{
        minWidth: '150px',
        width: '300px',
      }}
    >
      <MenuSection>
        {!isEmbed && <PictureInPictureMenuItem />}
        {!isEmbed && <TranscriptMenuItem />}
        {!isEmbed && <ScreenRecordingMenuItem />}
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
