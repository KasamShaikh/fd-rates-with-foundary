import React from 'react';

function ScrapeButton({ onClick, loading, disabled, selectedCount = 0, totalCount = 0 }) {
  const willFetchAll = selectedCount === 0;
  const label = loading
    ? 'Fetching...'
    : willFetchAll
    ? `🔍 Fetch All Banks${totalCount ? ` (${totalCount})` : ''}`
    : `🔍 Fetch Selected (${selectedCount})`;

  return (
    <button
      className="btn btn-primary"
      onClick={onClick}
      disabled={loading || disabled}
      title={
        willFetchAll
          ? 'No URLs selected — rates will be fetched for all configured URLs'
          : `Fetch rates only for the ${selectedCount} selected URL${selectedCount === 1 ? '' : 's'}`
      }
    >
      {loading && <span className="spinner" />}
      {label}
    </button>
  );
}

export default ScrapeButton;
