import { expect, mock, test } from 'claude-code/testing'

import type { ZentraleDoc } from '../../types'
import { paint } from './paint'
import { crewLine, lanes, rowLine } from './zentrale'

const BOARD = JSON.stringify({ updated: '2026-11-20T21:00:00Z', updated_by: 'samu', sessions: [{ name: 'samu', role: 'master' }] })

const row = (item: string, extra: Record<string, unknown> = {}) => ({
  kind: 'question', item, title: `title ${item}`, holder: 'owner', for: 'ABC-12-fix', master: 'alpha-7',
  since: '2026-11-20T20:20:00Z', since_note: '', age_s: 3000, age: '0h50', source: 'questions', classes: [], red: false, ...extra,
})

const session = (name: string, extra: Record<string, unknown> = {}) => ({
  name, session_id: `s-${name}`, kind: 'interactive', role: 'master', state: 'idle', doing: 'epic PROJ-1', keys: [], waiting_on: null,
  blocked_kind: null, since: null, age_s: 7800, age: '2h10', plugin: '9.17.0', starter: null, group: name, waits: false, ...extra,
})

const account = (name: string, extra: Record<string, unknown> = {}) => ({
  name, active: false, in_rotation: true, five_hour: { pct: 40, resets_at: null }, seven_day: { pct: 70, resets_at: null },
  threshold: 80, status: 'ok', measured_at: '2026-11-20T21:08:00Z', age_s: 120, stale: false, red: false, ...extra,
})

const doc = (fast: boolean, extra: Partial<ZentraleDoc['sections']> = {}, gaps: string[] = []) => ({
  schema_version: 1, project: 'acme', built_at: '2026-11-20T21:10:00Z', fast, header: ['Project: acme'], gaps,
  sources: {},
  sections: {
    owner: [row('Q4'), row('acme/app#24', { kind: 'pr', classes: ['user merge'], since_note: 'opened', age: '10h00' })],
    red: [row('acme/app#22', { kind: 'pr', holder: 'ABC-22-fix', for: '', classes: ['red'], red: true, age: '0h30' })],
    waiting: [row('beta-3', { kind: 'wait', holder: 'release (no session)', for: 'beta-3', master: '', title: '', age: '1h00' })],
    working: [session('lead')],
    ci: { repos: [], locks: 'locks: not read (flow.log_dir unset)' },
    accounts: [account('dev', { active: true }), account('two@example.com', { status: 'token_expired', red: true })],
    ...extra,
  },
})

const RUN = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })

const NOW = Date.parse('2026-11-20T21:10:30Z')

// The world of a session with the /board pane: a fake clock, store, files and processes. `full`
// decides what the slow, full read answers; `null` keeps it pending.
function world(on: Parameters<Parameters<typeof test>[1]>[1], full: () => Promise<unknown> | unknown) {
  const clock = mock.clock(on, { now: NOW })
  mock.store(on)
  on('env.get', () => ({ value: '/home/me' }))
  on('fs.exists', (_$, e) => ({ value: !String((e as { path?: unknown }).path).endsWith('.jsonl') }))
  on('fs.stat', () => ({ value: { kind: 'file', size: 1, mtimeMs: 1, isLink: false } }))
  on('fs.read', () => ({ value: BOARD }))
  const opened: string[] = []
  on('ui.open', (_$, e) => {
    opened.push(String((e as { id?: unknown }).id))
    return { value: { isPlaced: true } }
  })
  on('ui.log', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  const ran: string[][] = []
  on('process.run', async (_$, e) => {
    ran.push([...e.argv])
    const script = String(e.argv[2] ?? '')
    if (script.endsWith('/zentrale.py')) return e.argv.includes('--fast') ? RUN(JSON.stringify(doc(true, {}, ['github: loading']))) : full()
    if (script.endsWith('/flow_intervals.py')) return RUN(JSON.stringify({ intervals: INTERVALS }))
    if (e.argv[0] === 'python3') return RUN('{"sessions":[]}')
    return RUN('')
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: { command: 'board' } }))
  return { clock, opened, ran }
}

async function draw($: Parameters<Parameters<typeof test>[1]>[0], width = 110) {
  const ui = await $.ui.mount({ plugin: 'we', surface: 'terminal', component: 'Pane', requestId: 'we-zentrale', props: { bodyColumns: width } as never })
  const lines = paint((await ui.drawn()) as never, width).map(l => l.trimEnd())
  return { ui, lines }
}

async function openFromBoard($: Parameters<Parameters<typeof test>[1]>[0], clock: { settle: () => Promise<void> }) {
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'board', args: '', origin: { kind: 'composer' } } as never)
  await clock.settle()
  const board = await $.ui.mount({ plugin: 'we', surface: 'terminal', component: 'Pane', requestId: 'we-board', props: { bodyColumns: 100 } as never })
  await board.press({ key: 'zentrale' })
  await board.unmount()
}

test('rowLine names item, holder, for whom with the master, age and title', () => {
  expect(rowLine(row('Q4') as never)).toBe('Q4 · owner · for ABC-12-fix (alpha-7) · 0h50 · title Q4')
  expect(rowLine(row('ABC-15', { kind: 'story', since: 'unknown', for: '', master: '', title: '' }) as never)).toBe('ABC-15 · owner · since ?')
  expect(crewLine(session('samu', { role: 'coordinator', doing: 'Q-Triage PROJ-31', waiting_on: 'human', waits: true, age: '0h12' }) as never)).toBe(
    'samu · coordinator · Q-Triage PROJ-31 · since 0h12 · waits: human · 9.17.0',
  )
})

test('/board: Zentrale opens the sections in order, an empty one says nothing, a gap stands on top (AC12)', async ($, on) => {
  const { clock, opened, ran } = world(on, () => RUN(JSON.stringify(doc(false))))
  await openFromBoard($, clock)
  await clock.settle()
  expect(opened).toContain('we-zentrale')
  const calls = ran.filter(a => String(a[2] ?? '').endsWith('/zentrale.py'))
  expect(calls.map(a => a.slice(3))).toEqual([
    ['--json', '--fast', '--board', '/home/me/.claude/we-state/board.json'],
    ['--json', '--board', '/home/me/.claude/we-state/board.json'],
  ])
  const { ui, lines } = await draw($)
  await ui.unmount()
  expect(lines.filter(Boolean)).toEqual([
    'Zentrale · acme',
    'Project: acme',
    'With the owner',
    '  Q4 · owner · for ABC-12-fix (alpha-7) · 0h50 · title Q4',
    '  acme/app#24 · owner · for ABC-12-fix (alpha-7) · 10h00 opened · user merge · title acme/app#24',
    'Red',
    '  acme/app#22 · ABC-22-fix · (alpha-7) · 0h30 · red · title acme/app#22',
    'Waiting',
    '  beta-3 · release (no session) · for beta-3 · 1h00',
    'Working',
    '  lead · master · epic PROJ-1 · since 2h10 · 9.17.0',
    'CI',
    '  nothing',
    'Accounts',
    '  dev (active) · 5h 40 % · 7d 70 % · limit 80 % · ok',
    '  two@example.com · 5h 40 % · 7d 70 % · limit 80 % · token_expired',
    '[ Refresh ]  [ Swimlanes ]  as of 21:10 UTC',
  ])
})

test('the first draw waits on no slow source; a second read of a kind does not start while one runs', async ($, on) => {
  let release: (v: unknown) => void = () => undefined
  const pending = new Promise(resolve => {
    release = resolve
  })
  const { clock, ran } = world(on, () => pending)
  await openFromBoard($, clock)
  await clock.settle()
  // the full read hangs: the pane draws the fast document, its slow sources named as loading
  const first = await draw($)
  await first.ui.unmount()
  expect(first.lines).toContain('⚠ github: loading')
  expect(first.lines).toContain('[ Refresh ]  [ Swimlanes ]  as of 21:10 UTC (loading)')
  // the pane is drawn since the full read began, so the refresh tick would read: the running
  // read's flag keeps a second one from starting
  const fulls = () => ran.filter(a => String(a[2] ?? '').endsWith('/zentrale.py') && !a.includes('--fast')).length
  await clock.advance(30_000)
  expect(fulls()).toBe(1)
  release(RUN(JSON.stringify(doc(false))))
  await clock.settle()
  const after = await draw($)
  await after.ui.unmount()
  expect(after.lines).toContain('[ Refresh ]  [ Swimlanes ]  as of 21:10 UTC')
})

test('closed, the pane asks the read layer nothing; drawn again, it reads again', async ($, on) => {
  const { clock, ran } = world(on, () => RUN(JSON.stringify(doc(false))))
  await openFromBoard($, clock)
  await clock.settle()
  const reads = () => ran.filter(a => String(a[2] ?? '').endsWith('/zentrale.py')).length
  const before = reads()
  await clock.advance(30_000)
  await clock.advance(30_000)
  await clock.advance(120_000)
  expect(reads()).toBe(before)
  const { ui } = await draw($)
  await clock.advance(30_000)
  await ui.unmount()
  expect(reads()).toBe(before + 1)
})

// --- swimlanes (AC13) -----------------------------------------------------------------------

const iso = (hoursBefore: number) => new Date(NOW - hoursBefore * 3_600_000).toISOString().replace('.000Z', 'Z')
const INTERVALS = [
  { who: 'alpha-7', kind: 'working', start: iso(8), end: iso(4) },
  { who: 'alpha-7', kind: 'waiting:human', start: iso(4), end: iso(0) },
  { who: 'beta-3', kind: 'waiting:coordinator', start: iso(20), end: iso(2) },
  { who: 'beta-3', kind: 'idle', start: iso(2), end: iso(0) },
  { who: 'samu', kind: 'idle', start: iso(23), end: iso(0) },
  { who: 'samu', kind: 'holding:gates', start: iso(3), end: iso(1) },
]

test('lanes: one lane per session, one glyph per kind, waiting:human in a colour of its own', () => {
  const drawn = lanes(INTERVALS, NOW, 8, 8)
  expect(drawn.map(l => [l.who, l.cells.map(c => c.glyph).join('')])).toEqual([
    ['alpha-7', '████▓▓▓▓'],
    ['beta-3', '▒▒▒▒▒▒··'],
    ['samu', '········'],
  ])
  const colours = new Set(drawn.flatMap(l => l.cells.map(c => c.color)))
  expect([...colours].sort()).toEqual(['claude', 'inactive', 'success', 'warning'])
  const human = drawn[0]!.cells.at(-1)!.color
  expect(drawn.flatMap(l => l.cells).filter(c => c.color === human).every(c => c.glyph === '▓')).toBe(true)
  // a blocked or parked session and a wait on CI draw a glyph of their own, never an empty cell
  const more = lanes(
    [
      { who: 'w-stuck', kind: 'blocked', start: iso(8), end: iso(4) },
      { who: 'w-stuck', kind: 'waiting:ci', start: iso(4), end: iso(0) },
      { who: 'w-done', kind: 'idle-after-end', start: iso(8), end: iso(4) },
      { who: 'w-done', kind: 'parked', start: iso(4), end: iso(0) },
    ],
    NOW, 8, 4,
  )
  expect(more.map(l => [l.who, l.cells.map(c => c.glyph).join('')])).toEqual([
    ['w-done', '--__'],
    ['w-stuck', '▚▚░░'],
  ])
  expect(more[1]!.cells[0]!.color).toBe('error')
  // 24 h: beta-3's wait starts 20 h back
  expect(lanes(INTERVALS, NOW, 24, 12).find(l => l.who === 'beta-3')!.cells.map(c => c.glyph).join('')).toBe('  ▒▒▒▒▒▒▒▒▒·')
})

test('Swimlanes, then 2 and 1, switch the window (AC13)', async ($, on) => {
  const { clock, ran } = world(on, () => RUN(JSON.stringify(doc(false))))
  await openFromBoard($, clock)
  await clock.settle()
  const pane = await $.ui.mount({ plugin: 'we', surface: 'terminal', component: 'Pane', requestId: 'we-zentrale', props: { bodyColumns: 40 } as never })
  await pane.press({ key: 'lanes' })
  await clock.settle()
  await pane.unmount()
  expect(ran.some(a => String(a[2] ?? '').endsWith('/flow_intervals.py'))).toBe(true)
  let { ui, lines } = await draw($, 40)
  expect(lines).toContain('Swimlanes · last 8 h')
  expect(lines.some(l => l.startsWith('alpha-7 ') && l.includes('▓'))).toBe(true)
  expect(lines.some(l => l.startsWith('█ working  ▓ waits on the owner'))).toBe(true)
  await ui.press({ key: 'h24' })
  await clock.settle()
  await ui.unmount()
  ;({ ui, lines } = await draw($, 40))
  expect(lines).toContain('Swimlanes · last 24 h')
  await ui.press({ key: 'h8' })
  await clock.settle()
  await ui.unmount()
  ;({ ui, lines } = await draw($, 40))
  await ui.unmount()
  expect(lines).toContain('Swimlanes · last 8 h')
})
