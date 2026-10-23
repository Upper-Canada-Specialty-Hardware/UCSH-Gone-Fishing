import { ReactNode } from 'react';
import { Box, Paper, Skeleton, Typography } from '@mui/material';
import { KF } from './Motion';

/*
 * Loading skeletons for My requests and My team: the page's own shape, drawn
 * in placeholder bars that shimmer, so the real page lands in the same place.
 * The shimmer is a CSS animation, so the app's reduced-motion rule stills it.
 */

/** Hides text from the eye but not from screen readers. Sizes are px strings: in sx a bare 1 means 100%. */
const SR_ONLY = { position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap' } as const;

/** Tiles in a row, the same grid the dashboards use. */
const TILE_GRID = (min: number) => ({ display: 'grid', gap: 2, gridTemplateColumns: `repeat(auto-fit, minmax(min(${min}px, 100%), 1fr))` });

/** A placeholder bar. @param props - Its width and height, and any MUI Skeleton props. */
function Bar({ w, h = 14, ...rest }: { w: number | string; h?: number } & Omit<Parameters<typeof Skeleton>[0], 'width' | 'height'>) {
  return <Skeleton animation="wave" variant="rounded" width={w} height={h} {...rest} />;
}

/**
 * A bar centred in a line of text's height, so the skeleton takes the room
 * the real text will.
 *
 * @param props.lh - The line's height in px.
 * @param props.w - The bar's width.
 * @param props.h - The bar's height.
 * @returns The line.
 */
function Line({ lh, w, h }: { lh: number; w: number | string; h: number }) {
  return <Box sx={{ height: lh, display: 'flex', alignItems: 'center' }}><Bar w={w} h={h} /></Box>;
}

/**
 * The frame every skeleton shares: hidden from screen readers, which hear
 * "Loading" once instead. It shows after a short wait, so a quick load never
 * flashes it.
 *
 * @param props.label - What is loading, for screen readers.
 * @param props.children - The page's shape.
 * @returns The skeleton.
 */
function Frame({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Box role="status" sx={{ display: 'grid', gap: 2.5, animation: `${KF.fade} .3s ease-out .15s both` }}>
      <Box component="span" sx={SR_ONLY}>{label}</Box>
      <Box aria-hidden sx={{ display: 'contents' }}>{children}</Box>
    </Box>
  );
}

/** The tab strip: one bar per tab, on the divider line. @param props.count - How many tabs. */
function TabsBar({ count }: { count: number }) {
  return (
    <Box sx={{ display: 'flex', gap: 3, alignItems: 'center', height: 49, px: 2, borderBottom: 1, borderColor: 'divider', overflow: 'hidden' }}>
      {Array.from({ length: count }, (_, i) => <Bar key={i} w={i === 0 ? 92 : 64} />)}
    </Box>
  );
}

/**
 * A number tile: label, big number, note.
 *
 * @param props.pot - A balance tile (spaced a little wider than the others).
 * @param props.bar - Add a balance bar.
 * @param props.spark - Add a sparkline's space.
 * @param props.lines - Lines in its note.
 * @returns The tile.
 */
function Tile({ pot, bar, spark, lines = 1 }: { pot?: boolean; bar?: boolean; spark?: boolean; lines?: number }) {
  return (
    <Paper sx={{ p: 2, display: 'grid', gap: pot ? 0.5 : 0.25, alignContent: 'start' }}>
      <Line lh={20} w="55%" h={12} />
      <Line lh={37} w={64} h={28} />
      {bar && <Bar w="100%" h={6} />}
      {Array.from({ length: lines }, (_, i) => <Line key={i} lh={20} w={i ? '50%' : '85%'} h={10} />)}
      {spark && <Bar w="100%" h={34} />}
    </Paper>
  );
}

/** A titled panel with a block where its chart or table goes. @param props.h - The block's height. */
function PanelBlock({ h }: { h: number }) {
  return (
    <Paper sx={{ p: 2.25, display: 'grid', gap: 1.5 }}>
      <Bar w={160} h={16} />
      <Bar w="100%" h={h} />
    </Paper>
  );
}

/**
 * My requests while it loads: the greeting and record, the tabs, then the
 * open tab's shape (balances and lists, or tiles and charts).
 *
 * @param props.tab - The tab that will open.
 * @returns The skeleton.
 */
export function EmployeeSkeleton({ tab }: { tab: string }) {
  return (
    <Frame label="Loading your requests…">
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 220 }}>
          <Line lh={42} w={180} h={32} />
          <Box sx={{ mt: 1.25, display: 'flex', flexWrap: 'wrap', columnGap: 3.5, rowGap: 1.25 }}>
            {[90, 80, 70, 150, 120].map((w, i) => (
              <Box key={i} sx={{ display: 'grid', gap: 0.25 }}><Line lh={20} w={64} h={10} /><Line lh={20} w={w} h={14} /></Box>
            ))}
          </Box>
        </Box>
        <Bar w={170} h={42} />
      </Box>
      <TabsBar count={3} />
      {tab === 'overview' ? (
        <>
          <Box sx={TILE_GRID(160)}>{[1, 1, 0, 0, 0].map((b, i) => <Tile key={i} pot bar={!!b} lines={b ? 1 : 2} />)}</Box>
          <Paper sx={{ overflow: 'hidden', pb: 0.5 }}>
            {[1, 2, 4].map((rows, s) => (
              <Box key={s}>
                <Box sx={{ px: 2.25, pt: 2, pb: 0.5 }}><Line lh={24} w={170} h={14} /></Box>
                {Array.from({ length: rows }, (_, r) => (
                  <Box key={r} sx={{ display: 'flex', alignItems: 'center', gap: 1.25, px: 2.25, py: 1.25, borderTop: 1, borderColor: 'divider' }}>
                    <Bar w={96} h={24} sx={{ borderRadius: 999 }} />
                    <Box sx={{ flex: 1 }}><Bar w="45%" /></Box>
                    <Bar w={72} h={24} sx={{ borderRadius: 999 }} />
                  </Box>
                ))}
              </Box>
            ))}
          </Paper>
        </>
      ) : (
        <>
          <Box sx={TILE_GRID(160)}>{Array.from({ length: 5 }, (_, i) => <Tile key={i} />)}</Box>
          <PanelBlock h={tab === 'history' ? 420 : 240} />
        </>
      )}
    </Frame>
  );
}

/** One request waiting on a decision: who, when, the balance it moves, the buttons. */
function DecisionBlock() {
  return (
    <Paper sx={{ p: 2.25, display: 'grid', gap: 1.75 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
        <Skeleton animation="wave" variant="circular" width={44} height={44} />
        <Box sx={{ flex: 1, display: 'grid', gap: 0.75 }}><Bar w={130} h={16} /><Bar w={100} h={12} /></Box>
        <Bar w={76} h={24} sx={{ borderRadius: 999 }} />
      </Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
        <Bar w={56} h={58} /><Bar w={56} h={58} /><Bar w={60} h={18} />
      </Box>
      <Bar w="100%" h={8} />
      <Bar w={120} h={22} sx={{ borderRadius: 999 }} />
      <Box sx={{ display: 'flex', gap: 1 }}><Bar w={96} h={40} /><Bar w={84} h={40} /></Box>
    </Paper>
  );
}

/**
 * My team while it loads: the greeting, the tabs, then the open tab's shape
 * (tiles, the queue and the two weeks ahead, or a panel).
 *
 * @param props.tab - The tab that will open.
 * @returns The skeleton.
 */
export function TeamSkeleton({ tab }: { tab: string }) {
  return (
    <Frame label="Loading your team…">
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 240, maxWidth: 380 }}>
          <Line lh={42} w="80%" h={32} />
          <Line lh={24} w="100%" h={14} />
        </Box>
        <Bar w={140} h={38} />
      </Box>
      <TabsBar count={6} />
      {tab === 'today' ? (
        <>
          <Box sx={TILE_GRID(150)}>{[0, 0, 1, 1].map((sp, i) => <Tile key={i} spark={!!sp} />)}</Box>
          <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: { xs: 'minmax(0, 1fr)', lg: 'minmax(0, 1.1fr) minmax(0, 1fr)' }, alignItems: 'start' }}>
            <Box sx={{ display: 'grid', gap: 2 }}><DecisionBlock /><DecisionBlock /></Box>
            <Box sx={{ display: 'grid', gap: 2 }}>
              <Paper sx={{ p: 2.25, display: 'grid', gap: 1.25 }}>
                <Bar w={150} h={16} />
                {Array.from({ length: 4 }, (_, i) => (
                  <Box key={i} sx={{ display: 'grid', gridTemplateColumns: '96px 1fr', alignItems: 'center', gap: 1 }}><Bar w={60} /><Bar w="100%" h={24} /></Box>
                ))}
              </Paper>
              <PanelBlock h={110} />
            </Box>
          </Box>
        </>
      ) : (
        <PanelBlock h={tab === 'calendar' ? 360 : 300} />
      )}
    </Frame>
  );
}

/** A table's rows: a header line, then rows of cells. @param props.rows - How many rows. */
function TableRows({ rows }: { rows: number }) {
  return (
    <>
      <Box sx={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr) 90px', gap: 2, px: 2, py: 0.75, borderBottom: 1, borderColor: 'divider' }}>
        {[70, 50, 60, 40].map((w, i) => <Line key={i} lh={24} w={w} h={10} />)}
      </Box>
      {Array.from({ length: rows }, (_, r) => (
        <Box key={r} sx={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr) 90px', gap: 2, alignItems: 'center', px: 2, py: 2, borderBottom: 1, borderColor: 'divider' }}>
          <Line lh={24} w="70%" h={14} />
          <Line lh={24} w={96} h={22} />
          <Line lh={24} w="60%" h={12} />
          <Bar w={72} h={24} sx={{ borderRadius: 999 }} />
        </Box>
      ))}
    </>
  );
}

/**
 * The admin dashboard's main area while it loads, shaped like the screen
 * that will open: home's greeting, tiles and waiting list; the requests
 * table with its toolbar; or a panel. The screen's real title shows at once.
 *
 * @param props.screen - The screen that will open.
 * @param props.table - Whether it is a view of the requests table.
 * @param props.title - Its title, shown above every screen but home.
 * @returns The skeleton.
 */
export function AdminSkeleton({ screen, table, title }: { screen: string; table: boolean; title?: string }) {
  return (
    <Frame label="Loading the admin dashboard…">
      {title && <Typography variant="h5">{title}</Typography>}
      {screen === 'home' ? (
        <Box sx={{ display: 'grid', gap: 3 }}>
          <Box><Line lh={42} w="min(260px, 70%)" h={32} /><Line lh={24} w="min(340px, 90%)" h={14} /></Box>
          <Box sx={TILE_GRID(210)}>
            {Array.from({ length: 4 }, (_, i) => (
              <Paper key={i} sx={{ p: 2.25, display: 'grid', gap: 0.5 }}>
                <Line lh={20} w="55%" h={12} />
                <Line lh={37} w={56} h={28} />
                <Line lh={20} w="75%" h={10} />
              </Paper>
            ))}
          </Box>
          <Paper sx={{ overflow: 'hidden' }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', px: 2.5, py: 1.75, borderBottom: 1, borderColor: 'divider' }}>
              <Line lh={24} w={140} h={14} /><Line lh={24} w={60} h={12} />
            </Box>
            {Array.from({ length: 6 }, (_, i) => (
              // Phones drop the date column, as the real list does.
              <Box key={i} sx={{ display: 'grid', gridTemplateColumns: { xs: 'minmax(0,1fr) auto auto', sm: 'minmax(0,1.2fr) auto minmax(0,1fr) auto' }, gap: 2, alignItems: 'center', px: 2.5, py: 1.25, borderTop: 1, borderColor: 'divider' }}>
                <Line lh={24} w="60%" h={14} /><Bar w={84} h={22} sx={{ borderRadius: 999 }} />
                <Box sx={{ display: { xs: 'none', sm: 'block' } }}><Line lh={24} w="50%" h={12} /></Box>
                <Bar w={56} h={12} />
              </Box>
            ))}
          </Paper>
        </Box>
      ) : table ? (
        <>
        {/* Each view has a line above its table saying what it holds. */}
        <Box sx={{ mt: -1.5 }}><Line lh={24} w="min(420px, 80%)" h={14} /></Box>
        <Paper sx={{ overflow: 'hidden' }}>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center', p: 1.5, borderBottom: 1, borderColor: 'divider' }}>
            {[96, 140, 76, 64].map((w, i) => <Bar key={i} w={w} h={32} sx={{ borderRadius: 999 }} />)}
            <Box sx={{ flex: 1 }} />
            <Bar w={150} h={40} /><Bar w={200} h={40} sx={{ maxWidth: '100%' }} />
          </Box>
          <TableRows rows={8} />
        </Paper>
        </>
      ) : (
        <PanelBlock h={360} />
      )}
    </Frame>
  );
}
