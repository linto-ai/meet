import { Fragment, ReactNode } from 'react'
import { css } from '@/styled-system/css'
import { Text } from '@/primitives'

/**
 * A deliberately tiny Markdown renderer for LLM output.
 *
 * The catch-up summary is generated text: it must NEVER reach the DOM as HTML.
 * Instead of pulling a parser + sanitizer pair (the app ships neither), this
 * renders the small subset the prompt asks for — headings, bullets, bold/italic,
 * paragraphs — as React ELEMENTS, so every character is escaped by React and no
 * markup in the model's answer can ever execute.
 */

const INLINE = /(\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|`[^`\n]+`)/g

/** Split one line into plain / bold / italic / code runs. */
const inline = (line: string, keyPrefix: string): ReactNode[] =>
  line
    .split(INLINE)
    .filter((part) => part !== '')
    .map((part, i) => {
      const key = `${keyPrefix}-${i}`
      if (
        (part.startsWith('**') && part.endsWith('**')) ||
        (part.startsWith('__') && part.endsWith('__'))
      ) {
        return <strong key={key}>{part.slice(2, -2)}</strong>
      }
      if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
        return <code key={key}>{part.slice(1, -1)}</code>
      }
      if (
        part.length > 2 &&
        ((part.startsWith('*') && part.endsWith('*')) ||
          (part.startsWith('_') && part.endsWith('_')))
      ) {
        return <em key={key}>{part.slice(1, -1)}</em>
      }
      return <Fragment key={key}>{part}</Fragment>
    })

const listClass = css({
  paddingLeft: '1.1rem',
  listStyleType: 'disc',
  display: 'flex',
  flexDirection: 'column',
  gap: '0.15rem',
})

export const SimpleMarkdown = ({ text }: { text: string }) => {
  const blocks: ReactNode[] = []
  let bullets: string[] = []

  const flushBullets = () => {
    if (bullets.length === 0) return
    const items = bullets
    bullets = []
    blocks.push(
      <ul key={`ul-${blocks.length}`} className={listClass}>
        {items.map((item, i) => (
          <li key={i}>
            <Text variant="sm" as="span">
              {inline(item, `li-${blocks.length}-${i}`)}
            </Text>
          </li>
        ))}
      </ul>
    )
  }

  for (const rawLine of (text || '').split('\n')) {
    const line = rawLine.trim()
    if (!line) {
      flushBullets()
      continue
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      flushBullets()
      blocks.push(
        <Text
          key={`h-${blocks.length}`}
          variant="sm"
          as="p"
          className={css({ fontWeight: 'bold', marginTop: '0.35rem' })}
        >
          {inline(heading[2], `h-${blocks.length}`)}
        </Text>
      )
      continue
    }
    const bullet = /^[-*•]\s+(.*)$/.exec(line)
    if (bullet) {
      bullets.push(bullet[1])
      continue
    }
    flushBullets()
    blocks.push(
      <Text key={`p-${blocks.length}`} variant="sm" as="p">
        {inline(line, `p-${blocks.length}`)}
      </Text>
    )
  }
  flushBullets()

  return (
    <div
      className={css({
        display: 'flex',
        flexDirection: 'column',
        gap: '0.25rem',
      })}
    >
      {blocks}
    </div>
  )
}
