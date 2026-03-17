import { useState, useMemo, Fragment } from 'react';
import {
  Box, Chip, FormControl, InputLabel, Select, MenuItem, Button,
  Table, TableHead, TableBody, TableRow, TableCell, TablePagination,
  TableSortLabel, TableContainer, IconButton, Collapse, Typography, Paper,
} from '@mui/material';
import { KeyboardArrowDown, KeyboardArrowUp } from '@mui/icons-material';

interface Props {
  requests: any[];
  loading?: boolean;
  showEmployee?: boolean;
  onRefund?: (type: string, id: string) => void;
  processingEnabled?: boolean;
  actionLoading?: string | null;
}

const statusColor: Record<string, 'success' | 'error' | 'warning' | 'info' | 'default'> = {
  Approved: 'success',
  Rejected: 'error',
  Pending: 'warning',
  Refunded: 'info',
};

const BALANCE_LABELS: Record<string, string> = {
  CurrentOvertimeBalance: 'Make-Up',
  CurrentVacationBalance: 'Vacation',
  CurrentSickDayBalance: 'Sick',
  CarryOver: 'Carry Over',
  Payout: 'Payout',
};

type Order = 'asc' | 'desc';

function descendingComparator(a: any, b: any, orderBy: string) {
  const aVal = a[orderBy] ?? '';
  const bVal = b[orderBy] ?? '';
  if (bVal < aVal) return -1;
  if (bVal > aVal) return 1;
  return 0;
}

function getComparator(order: Order, orderBy: string) {
  return order === 'desc'
    ? (a: any, b: any) => descendingComparator(a, b, orderBy)
    : (a: any, b: any) => -descendingComparator(a, b, orderBy);
}

function getDisplayType(row: any): string {
  if (row.request_type === 'leave') return 'Leave';
  if (row.request_type === 'overtime') return 'Overtime';
  if (row.request_type === 'carryover-payout') return row.TypeofRequest || 'Carry Over / Payout';
  return row.request_type;
}

function getStartDate(row: any): string {
  return row.StartDate || (row.Created ? row.Created.split('T')[0] : '');
}

function hasAuditLog(row: any): boolean {
  const status = row.Status;
  if (status !== 'Approved' && status !== 'Refunded') return false;
  const log = row.BalanceAuditLog;
  if (!log || typeof log !== 'string' || !log.trim()) return false;
  try {
    const parsed = JSON.parse(log);
    return Array.isArray(parsed) && parsed.length > 0;
  } catch {
    return false;
  }
}

function AuditTrailPanel({ auditLog }: { auditLog: string }) {
  let entries: any[];
  try {
    entries = JSON.parse(auditLog);
  } catch {
    return <Typography color="error" variant="body2">Invalid audit data</Typography>;
  }

  if (!Array.isArray(entries) || entries.length === 0) return null;

  return (
    <Box sx={{ p: 2 }}>
      {entries.map((entry: any, i: number) => (
        <Box key={i} sx={{ mb: i < entries.length - 1 ? 2 : 0 }}>
          <Typography variant="subtitle2" sx={{ mb: 0.5, fontWeight: 600 }}>
            {entry.action === 'approve' ? 'Approved' : entry.action === 'refund' ? 'Refunded' : entry.action}
            {' — '}
            {entry.timestamp}
          </Typography>
          <Table size="small" sx={{ '& td, & th': { py: 0.5, px: 1 } }}>
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontWeight: 600 }}>Operation</TableCell>
                <TableCell sx={{ fontWeight: 600 }}>Changes</TableCell>
                <TableCell sx={{ fontWeight: 600 }}>Detail</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(entry.steps || []).map((step: any, j: number) => (
                <TableRow key={j}>
                  <TableCell>{step.operation}</TableCell>
                  <TableCell>
                    {Object.keys(step.before || {}).map((key) => {
                      const label = BALANCE_LABELS[key] || key;
                      const bv = step.before[key];
                      const av = step.after?.[key];
                      return (
                        <Box key={key} component="span" sx={{ display: 'block', whiteSpace: 'nowrap' }}>
                          {label}: {bv} &rarr; {av}
                        </Box>
                      );
                    })}
                  </TableCell>
                  <TableCell>{step.detail || ''}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      ))}
    </Box>
  );
}

interface ColumnDef {
  field: string;
  label: string;
  sortable?: boolean;
  width?: number;
  align?: 'left' | 'right' | 'center';
  getValue?: (row: any) => any;
  render?: (row: any) => React.ReactNode;
}

function RequestRow({
  row,
  columns,
  expandable,
  onRefund,
  processingEnabled,
  actionLoading,
}: {
  row: any;
  columns: ColumnDef[];
  expandable: boolean;
  onRefund?: (type: string, id: string) => void;
  processingEnabled?: boolean;
  actionLoading?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const colSpan = columns.length + (expandable || hasAuditLog(row) ? 1 : 0) + (onRefund ? 1 : 0);
  const rowExpandable = hasAuditLog(row);

  return (
    <Fragment>
      <TableRow hover sx={{ '& > *': { borderBottom: open ? 'unset' : undefined } }}>
        {expandable && (
          <TableCell sx={{ width: 48, p: 0.5 }}>
            {rowExpandable && (
              <IconButton size="small" onClick={() => setOpen(!open)}>
                {open ? <KeyboardArrowUp /> : <KeyboardArrowDown />}
              </IconButton>
            )}
          </TableCell>
        )}
        {columns.map((col) => (
          <TableCell key={col.field} align={col.align || 'left'}>
            {col.render ? col.render(row) : (col.getValue ? col.getValue(row) : (row[col.field] ?? ''))}
          </TableCell>
        ))}
        {onRefund && (
          <TableCell>
            {row.Status === 'Approved' && (() => {
              const key = `${row.request_type}-${row.id}`;
              return (
                <Button
                  size="small"
                  variant="outlined"
                  color="warning"
                  disabled={!processingEnabled || actionLoading === key}
                  onClick={() => onRefund(row.request_type, String(row.id))}
                >
                  {actionLoading === key ? 'Refunding...' : 'Refund'}
                </Button>
              );
            })()}
          </TableCell>
        )}
      </TableRow>
      {rowExpandable && (
        <TableRow>
          <TableCell sx={{ py: 0, pl: 6, pr: 2 }} colSpan={colSpan}>
            <Collapse in={open} timeout="auto" unmountOnExit>
              <AuditTrailPanel auditLog={row.BalanceAuditLog} />
            </Collapse>
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  );
}

export default function RequestHistory({ requests, loading, showEmployee, onRefund, processingEnabled, actionLoading }: Props) {
  const [typeFilter, setTypeFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [order, setOrder] = useState<Order>('desc');
  const [orderBy, setOrderBy] = useState('StartDate');
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(10);

  const filtered = useMemo(
    () => requests.filter((r) => {
      if (typeFilter && r.request_type !== typeFilter) return false;
      if (statusFilter && r.Status !== statusFilter) return false;
      return true;
    }),
    [requests, typeFilter, statusFilter],
  );

  const sorted = useMemo(
    () => [...filtered].sort(getComparator(order, orderBy)),
    [filtered, order, orderBy],
  );

  const paged = useMemo(
    () => sorted.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage),
    [sorted, page, rowsPerPage],
  );

  const handleSort = (field: string) => {
    const isAsc = orderBy === field && order === 'asc';
    setOrder(isAsc ? 'desc' : 'asc');
    setOrderBy(field);
  };

  // Any row in the dataset has an audit log?
  const anyExpandable = requests.some(hasAuditLog);

  const columns: ColumnDef[] = [
    {
      field: 'request_type', label: 'Type', sortable: true, width: 150,
      getValue: getDisplayType,
    },
    ...(showEmployee ? [{ field: 'employee_name', label: 'Employee', sortable: true, width: 180 } as ColumnDef] : []),
    { field: 'managers', label: 'Manager(s)', sortable: true, width: 200 },
    { field: 'LeaveType', label: 'Leave Type', sortable: true, width: 160 },
    {
      field: 'StartDate', label: 'Start', sortable: true, width: 120,
      getValue: getStartDate,
    },
    { field: 'EndDate', label: 'End', sortable: true, width: 120 },
    { field: 'Days', label: 'Days', sortable: true, width: 80, align: 'right' as const },
    { field: 'Hours', label: 'Hours', sortable: true, width: 80, align: 'right' as const },
    {
      field: 'Status', label: 'Status', sortable: true, width: 120,
      render: (row: any) => (
        <Chip label={row.Status || 'Unknown'} color={statusColor[row.Status as string] || 'default'} size="small" />
      ),
    },
    {
      field: 'Created', label: 'Created', sortable: true, width: 120,
      getValue: (row: any) => row.Created ? row.Created.split('T')[0] : '',
    },
    {
      field: 'ApprovedDate', label: 'Approved Date', sortable: true, width: 120,
      getValue: (row: any) => row.ApprovedDate ? row.ApprovedDate.split('T')[0] : '',
    },
  ];

  return (
    <Box>
      <Box sx={{ display: 'flex', gap: 2, mb: 2 }}>
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel>Type</InputLabel>
          <Select value={typeFilter} label="Type" onChange={(e) => setTypeFilter(e.target.value)}>
            <MenuItem value="">All</MenuItem>
            <MenuItem value="leave">Leave</MenuItem>
            <MenuItem value="overtime">Overtime</MenuItem>
            <MenuItem value="carryover-payout">Carry Over / Payout</MenuItem>
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel>Status</InputLabel>
          <Select value={statusFilter} label="Status" onChange={(e) => setStatusFilter(e.target.value)}>
            <MenuItem value="">All</MenuItem>
            <MenuItem value="Pending">Pending</MenuItem>
            <MenuItem value="Approved">Approved</MenuItem>
            <MenuItem value="Rejected">Rejected</MenuItem>
            <MenuItem value="Refunded">Refunded</MenuItem>
          </Select>
        </FormControl>
      </Box>
      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              {anyExpandable && <TableCell sx={{ width: 48 }} />}
              {columns.map((col) => (
                <TableCell
                  key={col.field}
                  align={col.align || 'left'}
                  sx={{ fontWeight: 600, width: col.width }}
                  sortDirection={orderBy === col.field ? order : false}
                >
                  {col.sortable ? (
                    <TableSortLabel
                      active={orderBy === col.field}
                      direction={orderBy === col.field ? order : 'asc'}
                      onClick={() => handleSort(col.field)}
                    >
                      {col.label}
                    </TableSortLabel>
                  ) : col.label}
                </TableCell>
              ))}
              {onRefund && <TableCell sx={{ fontWeight: 600, width: 120 }}>Actions</TableCell>}
            </TableRow>
          </TableHead>
          <TableBody>
            {loading ? (
              <TableRow>
                <TableCell colSpan={columns.length + (anyExpandable ? 1 : 0) + (onRefund ? 1 : 0)} align="center" sx={{ py: 4 }}>
                  Loading...
                </TableCell>
              </TableRow>
            ) : paged.length === 0 ? (
              <TableRow>
                <TableCell colSpan={columns.length + (anyExpandable ? 1 : 0) + (onRefund ? 1 : 0)} align="center" sx={{ py: 4 }}>
                  No requests found
                </TableCell>
              </TableRow>
            ) : (
              paged.map((row) => (
                <RequestRow
                  key={`${row.request_type}-${row.id}`}
                  row={row}
                  columns={columns}
                  expandable={anyExpandable}
                  onRefund={onRefund}
                  processingEnabled={processingEnabled}
                  actionLoading={actionLoading}
                />
              ))
            )}
          </TableBody>
        </Table>
        <TablePagination
          component="div"
          count={filtered.length}
          page={page}
          onPageChange={(_e, newPage) => setPage(newPage)}
          rowsPerPage={rowsPerPage}
          onRowsPerPageChange={(e) => { setRowsPerPage(parseInt(e.target.value, 10)); setPage(0); }}
          rowsPerPageOptions={[10, 25, 50]}
        />
      </TableContainer>
    </Box>
  );
}
