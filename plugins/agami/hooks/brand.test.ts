import { expect, test } from 'claude-code/testing'

import { ACCENT, BRAND, SOFT, logoSvg, logoSvgAnimated } from './brand'

// The engine refuses an `Svg` whose source is longer than this.
const SVG_LIMIT = 131_072

test('the brand colors are the website palette', () => {
  expect([BRAND, ACCENT, SOFT]).toEqual(['#2B38FC', '#0099FF', '#D2DBF1'])
})

test('both logos are the mark alone, in brand colors, with no script', () => {
  for (const svg of [logoSvg(), logoSvgAnimated()]) {
    expect(svg.startsWith('<svg')).toBe(true)
    expect(svg).toContain(BRAND)
    expect(svg).toContain(ACCENT)
    expect(svg.length).toBeLessThan(SVG_LIMIT / 10)
    expect(/<script|\son[a-z]+=|javascript:/i.test(svg)).toBe(false)
    // No wordmark and nothing fetched: no text, no font, no outside reference.
    expect(/<text|@font-face|@import|https?:\/\/(?!www\.w3\.org)/.test(svg)).toBe(false)
  }
})

test('the still logo is still and the animated one moves by CSS alone', () => {
  expect(logoSvg()).not.toContain('@keyframes')
  const moving = logoSvgAnimated()
  for (const name of ['agami-ring-draw', 'agami-bridge-pulse', 'agami-spin']) {
    expect(moving).toContain(`@keyframes ${name}`)
  }
  expect(moving).toContain('stroke-dashoffset')
  expect(moving).toContain('prefers-reduced-motion')
})
