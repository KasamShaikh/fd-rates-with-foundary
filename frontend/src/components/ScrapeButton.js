import React from 'react';

function ScrapeButton({ onClick, loading, disabled, selectedCount = 0, totalCount = 0 }) {
  const willScrapeAll = selectedCount === 0;
  const label = loading
    ? 'Scraping...'
    : willScrapeAll
    ? `🔍 Scrape All Banks${totalCount ? ` (${totalCount})` : ''}`
    : `🔍 Scrape Selected (${selectedCount})`;

  return (
    <button
      className="btn btn-primary"
      onClick={onClick}
      disabled={loading || disabled}
      title={
        willScrapeAll
          ? 'No URLs selected — all configured URLs will be scraped'
          : `Scrape only the ${selectedCount} selected URL${selectedCount === 1 ? '' : 's'}`
      }
    >
      {loading && <span className="spinner" />}
      {label}
    </button>
  );
}

export default ScrapeButton;
