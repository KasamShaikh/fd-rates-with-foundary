// ExportButton — triggers POST /api/export-excel which converts the latest
// fetch result into a styled .xlsx workbook (one sheet per bank) and uploads
// it to Blob Storage. Disabled until at least one fetch run has saved results.
import React from 'react';

function ExportButton({ onClick, loading, disabled }) {
  return (
    <button
      className="btn btn-success"
      onClick={onClick}
      disabled={loading || disabled}
    >
      {loading && <span className="spinner" />}
      {loading ? 'Exporting...' : '📊 Write Excel'}
    </button>
  );
}

export default ExportButton;
