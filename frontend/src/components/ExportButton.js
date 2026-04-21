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
