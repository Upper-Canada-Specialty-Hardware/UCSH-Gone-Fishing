import { useRef } from 'react';
import { Box } from '@mui/material';
import { useTheme } from '@mui/material/styles';

interface Props {
  /** The digits typed so far (0 to 6 characters). */
  value: string;
  /** Called with the new digits. */
  onChange: (code: string) => void;
  /** Called on Enter, so the code can be checked from the keyboard. */
  onEnter: () => void;
}

const LENGTH = 6;                                      // the emailed code's length

/**
 * The emailed code as six boxes, one digit each. Typing moves to the next box,
 * Backspace on an empty box goes back, and pasting (or a phone's one-time-code
 * suggestion) fills every box at once.
 *
 * @param props - See {@link Props}.
 * @returns The six inputs.
 */
export default function CodeBoxes({ value, onChange, onEnter }: Props) {
  const theme = useTheme();
  const refs = useRef<(HTMLInputElement | null)[]>([]);

  /** Put focus on one box, if it exists. @param i - Box index. */
  const focus = (i: number) => refs.current[Math.max(0, Math.min(LENGTH - 1, i))]?.focus();

  /**
   * Take what was typed or pasted into box i.
   *
   * @param i - Box index.
   * @param typed - The box's new content.
   */
  const handle = (i: number, typed: string) => {
    const old = value[i] ?? '';
    // A box that already held a digit now holds two: keep the new one.
    const fresh = old && typed.length === 2 ? (typed[0] === old ? typed[1] : typed[0]) : typed;
    const digits = fresh.replace(/\D/g, '');
    if (!digits) {                                     // box cleared
      onChange(value.slice(0, i) + value.slice(i + 1));
      return;
    }
    // Write from this box on (several digits: a paste or autofill), keeping any digits after them.
    const next = (value.slice(0, i) + digits + value.slice(i + digits.length)).slice(0, LENGTH);
    onChange(next);
    focus(Math.min(i + digits.length, next.length));   // the box after the last one written
  };

  return (
    <Box role="group" aria-label="6-digit code" sx={{ display: 'flex', gap: { xs: 0.75, sm: 1 } }}>
      {Array.from({ length: LENGTH }, (_, i) => (
        <Box
          key={i}
          component="input"
          ref={(el: HTMLInputElement | null) => { refs.current[i] = el; }}
          value={value[i] ?? ''}
          inputMode="numeric"
          autoComplete={i === 0 ? 'one-time-code' : 'off'}
          autoFocus={i === 0}
          aria-label={`Digit ${i + 1}`}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => handle(i, e.target.value)}
          onFocus={(e: React.FocusEvent<HTMLInputElement>) => e.target.select()}
          onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => {
            if (e.key === 'Backspace' && !value[i] && i > 0) focus(i - 1);   // step back over an empty box
            if (e.key === 'ArrowLeft') focus(i - 1);
            if (e.key === 'ArrowRight') focus(i + 1);
            if (e.key === 'Enter') onEnter();
          }}
          sx={{
            width: { xs: 42, sm: 52 }, height: { xs: 52, sm: 60 }, p: 0,
            textAlign: 'center', fontSize: 24, fontWeight: 600, fontFamily: theme.tokens.display,
            color: 'text.primary', bgcolor: 'background.paper',
            border: `1.5px solid ${theme.tokens.line2}`, borderRadius: '10px', outline: 'none',
            '&:focus': { borderColor: 'primary.main', boxShadow: `0 0 0 3px ${theme.tokens.primarySoft}` },
          }}
        />
      ))}
    </Box>
  );
}
