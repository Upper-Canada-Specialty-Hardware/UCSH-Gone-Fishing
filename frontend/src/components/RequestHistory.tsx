import { useState, useMemo, useEffect } from 'react';
import { Box, Chip, useMediaQuery } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { DataGrid, GridColDef, GridActionsCellItem, GridActionsCellItemProps, GridColumnVisibilityModel } from '@mui/x-data-grid';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import UndoIcon from '@mui/icons-material/Undo';
import AuditTrailDialog from './AuditTrailDialog';
import {
  SHARED_DATA_GRID_PROPS,
  STATUS_COLOR,
  REQUEST_TYPE_OPTIONS,
  STATUS_OPTIONS,
  getDisplayType,
  getDescription,
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

/** Columns worth hiding on a phone, where the full set would force a wide scroll. */
const HIDDEN_ON_NARROW: GridColumnVisibilityModel = {
  managers: false, LeaveType: false, description: false, EndDate: false, Hours: false, Created: false, ApprovedDate: false,
};

export default function RequestHistory({ requests, loading, showEmployee, onRefund, processingEnabled, actionLoading }: Props) {
  const [auditDialogLog, setAuditDialogLog] = useState<string | null>(null);
  const theme = useTheme();
  // True on phone-width screens; drives which columns start hidden.
  const narrow = useMediaQuery(theme.breakpoints.down('sm'));
  // The starting column set: phones hide the low-value columns; an employee's own
  // table hides Manager(s), which is the same on every row and named in the page header.
  const defaultVisibility = useMemo<GridColumnVisibilityModel>(
    () => ({ ...(narrow ? HIDDEN_ON_NARROW : {}), ...(showEmployee ? {} : { managers: false }) }),
    [narrow, showEmployee],
  );
  // Which columns are shown; starts from the default but the toolbar's Columns menu can still change it.
  const [columnVisibilityModel, setColumnVisibilityModel] = useState<GridColumnVisibilityModel>(defaultVisibility);
  // Crossing the phone breakpoint resets to that width's sensible default.
  useEffect(() => setColumnVisibilityModel(defaultVisibility), [defaultVisibility]);

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
        description: getDescription(r),
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
        // Roomy and minWidth-floored so an employee name is never clipped (it also wraps, see the grid sx).
        ? [{ field: 'employee_name', headerName: 'Employee', width: 200, minWidth: 160 } as GridColDef]
        : []),
      // Status right after who and what, so it is visible without scrolling sideways.
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
      { field: 'managers', headerName: 'Manager(s)', width: 200 },
      { field: 'LeaveType', headerName: 'Leave Type', width: 160 },
      { field: 'description', headerName: 'Description', width: 200 },
      { field: 'StartDate', headerName: 'Start', width: 120 },
      { field: 'EndDate', headerName: 'End', width: 120 },
      { field: 'Days', headerName: 'Days', width: 80, type: 'number' },
      { field: 'Hours', headerName: 'Hours', width: 80, type: 'number' },
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
    // width 100% + minWidth 0 keep the grid inside this box, so it scrolls internally and never widens the page.
    <Box sx={{ width: '100%', minWidth: 0 }}>
      <DataGrid
        rows={rows}
        columns={columns}
        loading={loading}
        columnVisibilityModel={columnVisibilityModel}
        onColumnVisibilityModelChange={setColumnVisibilityModel}
        getRowHeight={() => 'auto'}
        {...SHARED_DATA_GRID_PROPS}
        initialState={{
          ...SHARED_DATA_GRID_PROPS.initialState,
          sorting: { sortModel: [{ field: 'StartDate', sort: 'desc' }] },
        }}
        sx={{
          // Let text columns (Employee, Description, Manager) wrap instead of being cut; rows size to fit.
          '& .MuiDataGrid-cell': { whiteSpace: 'normal', lineHeight: 1.4, display: 'flex', alignItems: 'center', py: 0.75 },
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
