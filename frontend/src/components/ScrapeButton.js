// ScrapeButton — primary call-to-action that triggers a fetch run.
//
// The button text adapts to selection:
//   * 0 selected => "Fetch All Banks (N)" — the default, scrapes everything.
//   * N selected => "Fetch Selected (N)".
// While `loading` is true it shows a spinner and is disabled.
import React from 'react';

function ScrapeButton({ onClick, loading, disabled, selectedCount = 0, totalCount = 0 }) {
  // An empty selection in the sidebar means "fetch every configured URL".
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
