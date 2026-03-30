import { useState, useMemo } from 'react';
import { Box, Chip } from '@mui/material';
import { DataGrid, GridColDef, GridActionsCellItem, GridActionsCellItemProps } from '@mui/x-data-grid';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import UndoIcon from '@mui/icons-material/Undo';
import AuditTrailDialog from './AuditTrailDialog';
import {
  SHARED_DATA_GRID_PROPS,
  STATUS_COLOR,
  REQUEST_TYPE_OPTIONS,
  STATUS_OPTIONS,
  getDisplayType,
  getStartDate,
  hasAuditLog,
} from './dataGridDefaults';

interface Props {
  requests: any[];
  loading?: boolean;
  showEmployee?: boolean;
  onRefund?: (type: string, id: string) => void;
  processingEnabled?: boolean;
  actionLoading?: string | null;
}

export default function RequestHistory({ requests, loading, showEmployee, onRefund, processingEnabled, actionLoading }: Props) {
  const [auditDialogLog, setAuditDialogLog] = useState<string | null>(null);

  const rows = useMemo(
    () =>
      requests.map((r) => ({
        id: `${r.request_type}-${r.id}`,
        _raw: r,
        request_type: r.request_type,
        display_type: getDisplayType(r),
        employee_name: r.employee_name || '',
        managers: r.managers || '',
        LeaveType: r.LeaveType || '',
        StartDate: getStartDate(r),
        EndDate: r.EndDate || '',
        Days: r.Days ?? null,
        Hours: r.Hours ?? null,
        Status: r.Status || '',
        Created: r.Created ? r.Created.split('T')[0] : '',
        ApprovedDate: r.ApprovedDate ? r.ApprovedDate.split('T')[0] : '',
      })),
    [requests],
  );

  const columns = useMemo<GridColDef[]>(() => {
    const cols: GridColDef[] = [
      {
        field: 'display_type',
        headerName: 'Type',
        width: 150,
        type: 'singleSelect',
        valueOptions: REQUEST_TYPE_OPTIONS,
      },
      ...(showEmployee
        ? [{ field: 'employee_name', headerName: 'Employee', width: 180 } as GridColDef]
        : []),
      { field: 'managers', headerName: 'Manager(s)', width: 200 },
      { field: 'LeaveType', headerName: 'Leave Type', width: 160 },
      { field: 'StartDate', headerName: 'Start', width: 120 },
      { field: 'EndDate', headerName: 'End', width: 120 },
      { field: 'Days', headerName: 'Days', width: 80, type: 'number' },
      { field: 'Hours', headerName: 'Hours', width: 80, type: 'number' },
      {
        field: 'Status',
        headerName: 'Status',
        width: 120,
        type: 'singleSelect',
        valueOptions: STATUS_OPTIONS,
        renderCell: (params) => (
          <Chip
            label={params.value || 'Unknown'}
            color={STATUS_COLOR[params.value as string] || 'default'}
            size="small"
          />
        ),
      },
      { field: 'Created', headerName: 'Created', width: 120 },
      { field: 'ApprovedDate', headerName: 'Approved Date', width: 120 },
    ];

    const needsActions = requests.some(hasAuditLog) || !!onRefund;
    if (needsActions) {
      cols.push({
        field: 'actions',
        headerName: 'Actions',
        type: 'actions',
        width: 120,
        getActions: (params) => {
          const raw = params.row._raw;
          const actions: React.ReactElement<GridActionsCellItemProps>[] = [];

          if (hasAuditLog(raw)) {
            actions.push(
              <GridActionsCellItem
                key="audit"
                icon={<InfoOutlinedIcon />}
                label="Audit Trail"
                onClick={() => setAuditDialogLog(raw.BalanceAuditLog)}
              />,
            );
          }

          if (onRefund && raw.Status === 'Approved') {
            const rowKey = `${raw.request_type}-${raw.id}`;
            actions.push(
              <GridActionsCellItem
                key="refund"
                icon={<UndoIcon color="warning" />}
                label="Refund"
                disabled={!processingEnabled || actionLoading === rowKey}
                onClick={() => onRefund(raw.request_type, String(raw.id))}
              />,
            );
          }

          return actions;
        },
      });
    }

    return cols;
  }, [showEmployee, onRefund, processingEnabled, actionLoading, requests]);

  return (
    <Box>
      <DataGrid
        rows={rows}
        columns={columns}
        loading={loading}
        {...SHARED_DATA_GRID_PROPS}
        initialState={{
          ...SHARED_DATA_GRID_PROPS.initialState,
          sorting: { sortModel: [{ field: 'StartDate', sort: 'desc' }] },
        }}
      />
      <AuditTrailDialog
        open={auditDialogLog !== null}
        onClose={() => setAuditDialogLog(null)}
        auditLog={auditDialogLog || ''}
      />
    </Box>
  );
}
