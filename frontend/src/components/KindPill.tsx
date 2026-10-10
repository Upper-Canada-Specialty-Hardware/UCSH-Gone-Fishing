import { Box } from '@mui/material';
import { alpha, useTheme } from '@mui/material/styles';
import { kindColor, RequestKind } from '../constants/leaveColors';

/**
 * A small rounded label in the colour of its kind of request, so a vacation
 * or an overtime looks the same on every screen.
 *
 * @param props.kind - The kind (null shows a neutral pill).
 * @param props.label - The text.
 * @returns The pill.
 */
export default function KindPill({ kind, label }: { kind: RequestKind | null; label: string }) {
  const theme = useTheme();
  const c = kindColor(kind, theme.palette.mode);                  // the kind's shade for light or dark
  return (
    <Box
      component="span"
      sx={{
        display: 'inline-block', px: 1.25, py: 0.25, borderRadius: 999,
        fontSize: 12, fontWeight: 600, whiteSpace: 'nowrap',
        color: c, bgcolor: alpha(c, theme.palette.mode === 'dark' ? 0.18 : 0.12),  // tint behind the text
      }}
    >
      {label}
    </Box>
  );
}
