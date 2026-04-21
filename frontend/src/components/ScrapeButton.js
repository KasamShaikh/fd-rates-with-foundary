import React from 'react';

function ScrapeButton({ onClick, loading, disabled }) {
  return (
    <button
      className="btn btn-primary"
      onClick={onClick}
      disabled={loading || disabled}
    >
      {loading && <span className="spinner" />}
      {loading ? 'Scraping...' : '🔍 Scrape All Banks'}
    </button>
  );
}

export default ScrapeButton;
