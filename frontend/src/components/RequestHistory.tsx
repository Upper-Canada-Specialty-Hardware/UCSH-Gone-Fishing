import { useState, useMemo, useEffect } from 'react';
import { Box, Chip, useMediaQuery } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { DataGrid, GridColDef, GridActionsCellItem, GridActionsCellItemProps, GridColumnVisibilityModel } from '@mui/x-data-grid';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import UndoIcon from '@mui/icons-material/Undo';
import HistoryIcon from '@mui/icons-material/History';
import AuditTrailDialog from './AuditTrailDialog';
import RequestTimelineDialog from './RequestTimelineDialog';
import { LiveScope } from '../api/events';
import TakenFrom from './TakenFrom';
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
import { formatDay, formatInstantDay } from '../utils/dates';

interface Props {
  requests: any[];
  loading?: boolean;
  showEmployee?: boolean;
  onRefund?: (type: string, id: string) => void;
  processingEnabled?: boolean;
  actionLoading?: string | null;
  /** Which timeline endpoint the per-row timeline reads ('me' by default). */
  timelineScope?: LiveScope;
}

/** Columns worth hiding on a phone, where the full set would force a wide scroll. */
const HIDDEN_ON_NARROW: GridColumnVisibilityModel = {
  // Taken from wraps its chips but is still secondary, so it steps aside on a phone with the other low-value columns.
  managers: false, LeaveType: false, description: false, taken_from: false, EndDate: false, Hours: false, Created: false, ApprovedDate: false,
};

export default function RequestHistory({ requests, loading, showEmployee, onRefund, processingEnabled, actionLoading, timelineScope = 'me' }: Props) {
  const [auditDialogLog, setAuditDialogLog] = useState<string | null>(null);
  // The request whose timeline is open, or null when the dialog is closed.
  const [timelineFor, setTimelineFor] = useState<{ type: string; id: string } | null>(null);
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
        // The live stage words (Postgres mode); blank in SharePoint mode.
        stage: r.stage || '',
        // Keep the full ISO so the grid still sorts by the true instant; the
        // column's valueFormatter renders the reader's local day (no UTC shift).
        Created: r.Created || '',
        ApprovedDate: r.ApprovedDate || '',
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
      // The live stage, right after status, so progress is visible without scrolling.
      { field: 'stage', headerName: 'Stage', width: 180 },
      { field: 'managers', headerName: 'Manager(s)', width: 200 },
      { field: 'LeaveType', headerName: 'Leave Type', width: 160 },
      { field: 'description', headerName: 'Description', width: 200 },
      // Which balances the request's days came from (approved) or would (pending). Reads
      // the whole taken_from object off the raw row, so it is not sortable or filterable.
      {
        field: 'taken_from',
        headerName: 'Taken from',
        width: 220,
        sortable: false,
        filterable: false,
        renderCell: (params) => <TakenFrom value={params.row._raw?.taken_from} compact />,
      },
      // Start/End are calendar dates: read the date part as written, no shift.
      { field: 'StartDate', headerName: 'Start', width: 130, valueFormatter: (value) => formatDay(value as string) },
      { field: 'EndDate', headerName: 'End', width: 130, valueFormatter: (value) => formatDay(value as string) },
      { field: 'Days', headerName: 'Days', width: 80, type: 'number' },
      { field: 'Hours', headerName: 'Hours', width: 80, type: 'number' },
      // Created/Approved are instants: show the reader's local day of that moment.
      { field: 'Created', headerName: 'Created', width: 130, valueFormatter: (value) => formatInstantDay(value as string) },
      { field: 'ApprovedDate', headerName: 'Approved Date', width: 130, valueFormatter: (value) => formatInstantDay(value as string) },
    ];

    // Always shown: every row offers its timeline; audit and refund add to it.
    {
      cols.push({
        field: 'actions',
        headerName: 'Actions',
        type: 'actions',
        width: 140,
        getActions: (params) => {
          const raw = params.row._raw;
          const actions: React.ReactElement<GridActionsCellItemProps>[] = [];

          // Opening a request shows its timeline (#329).
          actions.push(
            <GridActionsCellItem
              key="timeline"
              icon={<HistoryIcon />}
              label="Timeline"
              onClick={() => setTimelineFor({ type: raw.request_type, id: String(raw.id) })}
            />,
          );

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
      <RequestTimelineDialog
        open={timelineFor !== null}
        onClose={() => setTimelineFor(null)}
        scope={timelineScope}
        requestType={timelineFor?.type ?? null}
        itemId={timelineFor?.id ?? null}
      />
    </Box>
  );
}
