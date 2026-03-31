import { GridToolbar } from '@mui/x-data-grid';

export const STATUS_COLOR: Record<string, 'success' | 'error' | 'warning' | 'info' | 'default'> = {
  Approved: 'success',
  Rejected: 'error',
  Pending: 'warning',
  Refunded: 'info',
};

export const REQUEST_TYPE_OPTIONS = [
  { value: 'Leave', label: 'Leave' },
  { value: 'Overtime', label: 'Overtime' },
  { value: 'Carry Over / Payout', label: 'Carry Over / Payout' },
];

export const STATUS_OPTIONS = ['Pending', 'Approved', 'Rejected', 'Refunded'];

export function getDisplayType(row: any): string {
  if (row.request_type === 'leave') return 'Leave';
  if (row.request_type === 'overtime') return 'Overtime';
  if (row.request_type === 'carryover-payout') return row.TypeofRequest || 'Carry Over / Payout';
  return row.request_type;
}

export function getDescription(row: any): string {
  const title = (row.Title || '').trim();
  if (!title) return '';
  if (row.request_type === 'overtime') return title;
  if (row.request_type === 'leave') {
    const parts = title.split(' /// ');
    return parts.length > 1 ? parts.slice(1).join(' /// ').trim() : '';
  }
  return title;
}

export function getStartDate(row: any): string {
  return row.StartDate || (row.Created ? row.Created.split('T')[0] : '');
}

export function hasAuditLog(row: any): boolean {
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

export const SHARED_DATA_GRID_PROPS = {
  pageSizeOptions: [10, 25, 50],
  initialState: { pagination: { paginationModel: { pageSize: 10 } } },
  autoHeight: true,
  disableRowSelectionOnClick: true,
  slots: { toolbar: GridToolbar },
  slotProps: { toolbar: { showQuickFilter: true } },
} as const;
