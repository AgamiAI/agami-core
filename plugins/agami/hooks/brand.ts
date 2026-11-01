// agami's colors and logo mark for the pane. Colors are the website palette; the mark is the one in
// `shared/agami-logo-dark.svg` without the wordmark (pane text carries the name in the app's own font).

export const BRAND = '#2B38FC'
export const ACCENT = '#0099FF'
export const SOFT = '#D2DBF1'

// The mark sits in a 237-unit square at (20, 38.5) in the source file's coordinates; the viewBox
// crops to it so the paths below are copied from that file unchanged.
const VIEW_BOX = '20 38.5 237 237'
const CENTER = '138.5px 157px'

// The source draws each ring as a whole circle clipped to the square, so only one quarter shows.
// Here each ring is that quarter alone, an arc of the same circle, so a dash animation draws the
// part that is seen instead of spending most of its time off-square.
const RINGS = [
  'M257.5 157A119 119 0 0 1 138.5 38',
  'M19.5 157A119 119 0 0 1 138.5 276',
]

// The two "bridges" where the rings meet. The source fills them with an inside stroke through a
// mask; a plain fill of the same outline draws the same shape with no mask to keep in sync.
const BRIDGES = [
  'M19.5 178.5C37.9507 178.5 56.2208 174.866 73.267 167.805C90.3133 160.744 105.802 150.395 118.848 137.349L88.6595 107.16C79.5774 116.242 68.7953 123.446 56.9289 128.361C45.0625 133.276 32.3441 135.806 19.5 135.806V178.5Z',
  'M257.5 135.5C239.049 135.5 220.779 139.134 203.733 146.195C186.687 153.256 171.198 163.605 158.151 176.652L188.563 207.063C197.616 198.01 208.363 190.828 220.191 185.929C232.02 181.03 244.697 178.508 257.5 178.508V135.5Z',
]

// Rings are drawn with `pathLength="100"` so the dash animation needs no arc lengths.
const RING_DASH = 100

// Colors are attributes, not CSS: the still logo has no stylesheet, and the animated one only
// animates. `prefers-reduced-motion` leaves the finished, still mark.
const STYLE = `
.agami-ring { stroke-dasharray: ${RING_DASH}; stroke-dashoffset: ${RING_DASH};
  animation: agami-ring-draw 1.2s ease-out forwards; }
.agami-ring-2 { animation-delay: 0.3s; }
.agami-bridge { animation: agami-bridge-pulse 2.4s ease-in-out 1.5s infinite; }
.agami-bridge-2 { animation-delay: 2.7s; }
.agami-turn { transform-box: view-box; transform-origin: ${CENTER};
  animation: agami-spin 30s linear infinite; }
@keyframes agami-ring-draw { to { stroke-dashoffset: 0; } }
@keyframes agami-bridge-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
@keyframes agami-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .agami-ring, .agami-bridge, .agami-turn { animation: none; stroke-dashoffset: 0; }
}`

const mark = (isAnimated: boolean): string => {
  const cls = (name: string, i: number): string =>
    isAnimated ? ` class="agami-${name} agami-${name}-${i + 1}"` : ''
  const rings = RINGS.map(
    (d, i) =>
      `<path${cls('ring', i)} d="${d}" pathLength="${RING_DASH}" stroke="${BRAND}" stroke-width="43" fill="none"/>`,
  ).join('')
  const bridges = BRIDGES.map((d, i) => `<path${cls('bridge', i)} d="${d}" fill="${ACCENT}"/>`).join('')
  const style = isAnimated ? `<style>${STYLE}</style>` : ''
  const turn = isAnimated ? ' class="agami-turn"' : ''

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${VIEW_BOX}" fill="none">${style}` +
    `<defs><clipPath id="agami-mark"><rect x="20" y="38.5" width="237" height="237"/></clipPath></defs>` +
    `<g${turn}><g clip-path="url(#agami-mark)">${bridges}${rings}</g></g></svg>`
  )
}

/** The mark, still: for surfaces that draw an `Svg` as an image (VS Code). */
export const logoSvg = (): string => mark(false)

/** The mark with CSS motion (no script): rings draw in, bridges pulse in turn, a slow turn. */
export const logoSvgAnimated = (): string => mark(true)
