import { Box, ButtonBase, Divider, Drawer, Typography } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';

/** One entry in the admin sidebar. */
export interface NavItem {
  /** The screen it opens (also the url segment). */
  key: string;
  label: string;
  /** A count shown at the right, highlighted when above zero. */
  count?: number;
}

/** A group of entries; a group with no title sits below a divider (the technical tools). */
export interface NavGroup {
  title?: string;
  items: NavItem[];
}

interface Props {
  groups: NavGroup[];
  /** The open screen. */
  current: string;
  onSelect: (key: string) => void;
  /** Small screens: whether the slide-out menu is open. */
  mobileOpen: boolean;
  onCloseMobile: () => void;
}

/**
 * The admin sidebar: always shown on wide screens, a slide-out menu on narrow
 * ones. Groups follow how HR thinks about the work; the last group has no
 * heading and sits under a divider, so the technical screens are there for
 * whoever needs them without being labelled as such.
 *
 * @param props - See {@link Props}.
 * @returns The sidebar.
 */
export default function AdminNav({ groups, current, onSelect, mobileOpen, onCloseMobile }: Props) {
  const theme = useTheme();

  const list = (
    <Box component="nav" aria-label="Admin" sx={{ p: 1.5, display: 'grid', gap: 0.25 }}>
      {groups.map((g, gi) => (
        <Box key={gi} sx={{ display: 'grid', gap: 0.25, mb: 1 }}>
          {/* A titled group gets a small caps heading; an untitled one, a divider. */}
          {g.title ? (
            <Typography variant="caption" sx={{ px: 1.5, pt: 1, pb: 0.5, color: 'text.secondary', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase' }}>
              {g.title}
            </Typography>
          ) : gi > 0 ? <Divider sx={{ my: 1 }} /> : null}
          {g.items.map((it) => {
            const on = it.key === current;
            return (
              <ButtonBase
                key={it.key}
                onClick={() => { onSelect(it.key); onCloseMobile(); }}
                aria-current={on ? 'page' : undefined}
                sx={{
                  justifyContent: 'space-between', gap: 1, px: 1.5, py: 1, borderRadius: '10px',
                  fontSize: 14, fontWeight: on ? 700 : 500, textAlign: 'left',
                  color: on ? 'primary.main' : 'text.primary',
                  bgcolor: on ? theme.tokens.primarySoft : 'transparent',
                  '&:hover': { bgcolor: on ? theme.tokens.primarySoft : theme.tokens.hover },
                }}
              >
                <span>{it.label}</span>
                {it.count !== undefined && (
                  <Box
                    component="span"
                    sx={{
                      minWidth: 22, px: 0.75, borderRadius: 999, fontSize: 12, fontWeight: 700, textAlign: 'center',
                      // Something waiting: terracotta; nothing: quiet grey.
                      color: it.count > 0 ? theme.tokens.accent : 'text.secondary',
                      bgcolor: it.count > 0 ? alpha(theme.tokens.accent, 0.14) : theme.tokens.chip,
                    }}
                  >
                    {it.count}
                  </Box>
                )}
              </ButtonBase>
            );
          })}
        </Box>
      ))}
    </Box>
  );

  return (
    <>
      {/* Wide screens: a fixed column beside the content. */}
      <Box
        sx={{
          display: { xs: 'none', md: 'block' }, width: 248, flex: 'none',
          borderRight: 1, borderColor: 'divider', bgcolor: 'background.paper',
          position: 'sticky', top: 56, height: 'calc(100vh - 56px)', overflowY: 'auto',
        }}
      >
        {list}
      </Box>
      {/* Narrow screens: the same list in a slide-out drawer. */}
      <Drawer open={mobileOpen} onClose={onCloseMobile} sx={{ display: { md: 'none' } }} PaperProps={{ sx: { width: 270 } }}>
        {list}
      </Drawer>
    </>
  );
}
