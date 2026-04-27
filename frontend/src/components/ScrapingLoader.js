import React from 'react';

/**
 * Animated placeholder shown in the dashboard area while a fetch is running
 * and no results have been loaded yet.
 */
function ScrapingLoader({ targetCount, eventCount }) {
  return (
    <div className="card scraping-loader">
      <div className="scraping-loader-spinner" aria-hidden="true">
        <div className="spinner-ring" />
      </div>
      <h2>Fetching FD rates…</h2>
      <p className="scraping-loader-sub">
        {targetCount > 0
          ? `Reading ${targetCount} bank page${targetCount === 1 ? '' : 's'} — this usually takes a few minutes.`
          : 'Reading bank pages — this usually takes a few minutes.'}
      </p>
      <div className="scraping-loader-bar" aria-hidden="true">
        <div className="scraping-loader-bar-fill" />
      </div>
      <p className="scraping-loader-hint">
        {eventCount > 0
          ? `${eventCount} live activity event${eventCount === 1 ? '' : 's'} so far. Watch the panel above.`
          : 'Waiting for the first event from the backend…'}
      </p>
    </div>
  );
}

export default ScrapingLoader;
