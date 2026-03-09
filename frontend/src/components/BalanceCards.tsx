import { Card, CardContent, Typography, LinearProgress, Box } from '@mui/material';
import Grid from '@mui/material/Grid2';

interface Balances {
  vacation_balance: number;
  vacation_entitlement: number;
  sick_balance: number;
  sick_entitlement: number;
  overtime: number;
  carryover: number;
  payout?: number;
}

interface Props {
  balances: Balances;
}

interface BalanceCardProps {
  label: string;
  balance: number;
  entitlement?: number;
  color: string;
  lowColor: string;
}

function BalanceCard({ label, balance, entitlement, color, lowColor }: BalanceCardProps) {
  const pct = entitlement ? Math.max(0, Math.min(100, (balance / entitlement) * 100)) : 100;
  const isLow = entitlement ? balance / entitlement < 0.2 : balance < 0;
  const barColor = isLow ? lowColor : color;

  return (
    <Card sx={{ height: '100%' }}>
      <CardContent>
        <Typography variant="subtitle2" color="text.secondary" gutterBottom>
          {label}
        </Typography>
        <Typography variant="h4" sx={{ color: isLow ? lowColor : 'text.primary', fontWeight: 600 }}>
          {balance}
          {entitlement != null && (
            <Typography component="span" variant="body2" color="text.secondary" sx={{ ml: 0.5 }}>
              / {entitlement}
            </Typography>
          )}
        </Typography>
        {entitlement != null && (
          <Box sx={{ mt: 1.5 }}>
            <LinearProgress
              variant="determinate"
              value={pct}
              sx={{
                height: 8,
                borderRadius: 4,
                backgroundColor: '#e5e7eb',
                '& .MuiLinearProgress-bar': { backgroundColor: barColor, borderRadius: 4 },
              }}
            />
          </Box>
        )}
      </CardContent>
    </Card>
  );
}

export default function BalanceCards({ balances }: Props) {
  return (
    <Grid container spacing={2}>
      <Grid size={{ xs: 12, sm: 6, md: 3 }}>
        <BalanceCard
          label="Vacation"
          balance={balances.vacation_balance}
          entitlement={balances.vacation_entitlement}
          color="#2563eb"
          lowColor="#dc2626"
        />
      </Grid>
      <Grid size={{ xs: 12, sm: 6, md: 3 }}>
        <BalanceCard
          label="Sick / Personal"
          balance={balances.sick_balance}
          entitlement={balances.sick_entitlement}
          color="#16a34a"
          lowColor="#dc2626"
        />
      </Grid>
      <Grid size={{ xs: 12, sm: 6, md: 3 }}>
        <BalanceCard
          label="Time Make-Up"
          balance={balances.overtime}
          color="#7c3aed"
          lowColor="#dc2626"
        />
      </Grid>
      <Grid size={{ xs: 12, sm: 6, md: 3 }}>
        <BalanceCard
          label="Carry Over"
          balance={balances.carryover}
          color="#0891b2"
          lowColor="#dc2626"
        />
      </Grid>
    </Grid>
  );
}
