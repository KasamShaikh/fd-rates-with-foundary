// =====================================================================
// App.js — top-level React component.
//
// Owns all global UI state (URL list, fetch results, progress events,
// selection, status message) and orchestrates calls to the backend API:
//   GET    /api/urls                — list configured bank URLs
//   POST   /api/urls                — add a URL
//   DELETE /api/urls/:id            — remove a URL
//   POST   /api/scrape              — kick off a fetch run (optionally with `ids`)
//   GET    /api/scrape/progress     — poll live progress events
//   GET    /api/results/latest      — fetch the most-recent saved run
//   DELETE /api/results/latest      — clear the saved run (Reset Screen)
//   POST   /api/export-excel        — generate styled Excel of latest results
// =====================================================================
import React, { useState, useEffect, useCallback, useRef } from 'react';
import UrlManager from './components/UrlManager';
import ScrapeButton from './components/ScrapeButton';
import ExportButton from './components/ExportButton';
import ResultsDashboard from './components/ResultsDashboard';
import ProgressLog from './components/ProgressLog';
import './App.css';

// Empty default => same-origin requests (works when frontend is served by the
// Function App). For local dev we set REACT_APP_API_BASE_URL=http://localhost:7071
// in .env so the React dev server (:3000) can talk to the Flask backend (:7071).
const API = process.env.REACT_APP_API_BASE_URL || '';

function App() {
  // --- Global UI state -------------------------------------------------
  const [urls, setUrls] = useState([]);              // configured bank URLs
  const [results, setResults] = useState(null);      // last fetch payload
  const [scraping, setScraping] = useState(false);   // a fetch run is active
  const [stopping, setStopping] = useState(false);   // Stop button has been hit; awaiting backend wind-down
  const [exporting, setExporting] = useState(false); // Excel export in flight
  const [message, setMessage] = useState('');        // top status banner text
  const [progressEvents, setProgressEvents] = useState([]); // live activity feed
  const [selectedIds, setSelectedIds] = useState([]);// URL ids ticked in sidebar
  const [forceRefresh, setForceRefresh] = useState(false); // bypass L1 HTTP cache
  const progressPollRef = useRef(null);              // setInterval handle for progress polling

  const fetchUrls = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/urls`);
      setUrls(await res.json());
    } catch { setMessage('Failed to load URLs'); }
  }, []);

  const fetchResults = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/results/latest`);
      if (res.ok) setResults(await res.json());
    } catch { /* no results yet */ }
  }, []);

  useEffect(() => { fetchUrls(); fetchResults(); }, [fetchUrls, fetchResults]);

  const addUrl = async (url, bankName) => {
    const res = await fetch(`${API}/api/urls`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, bank_name: bankName }),
    });
    if (res.ok) { setMessage('URL added'); fetchUrls(); }
    else setMessage('Failed to add URL');
  };

  const deleteUrl = async (id) => {
    await fetch(`${API}/api/urls/${id}`, { method: 'DELETE' });
    fetchUrls();
  };

  // Poll /api/scrape/progress until the backend reports running:false.
  // Returns true if it finished within the wait window, false otherwise.
  const waitForBackendIdle = async (maxWaitMs = 30 * 60 * 1000) => {
    const start = Date.now();
    while (Date.now() - start < maxWaitMs) {
      try {
        const r = await fetch(`${API}/api/scrape/progress`);
        if (r.ok) {
          const data = await r.json();
          if (data.events && data.events.length > 0) {
            // Replace events with the full latest snapshot so the UI catches up.
            setProgressEvents(data.events);
          }
          if (!data.running) return true;
        }
      } catch { /* keep retrying */ }
      await new Promise((res) => setTimeout(res, 2000));
    }
    return false;
  };

  const scrapeAll = async () => {
    setScraping(true);
    // Wipe the previous run's UI state so the user only sees this run's activity.
    setProgressEvents([]);
    setResults(null);
    const willScrapeAll = selectedIds.length === 0;
    const targetCount = willScrapeAll ? urls.length : selectedIds.length;
    setMessage(
      willScrapeAll
        ? `Fetching all ${urls.length} URL${urls.length === 1 ? '' : 's'}... watch the live activity below.`
        : `Fetching ${targetCount} selected URL${targetCount === 1 ? '' : 's'}... watch the live activity below.`
    );

    // Start polling progress every 1.5s. We track the backend's run_id and
    // ignore any events from an earlier run that may still be in the buffer
    // before the backend's reset() takes effect.
    let knownTotal = 0;
    let currentRunId = null;
    const poll = async () => {
      try {
        const r = await fetch(`${API}/api/scrape/progress?since=${knownTotal}`);
        if (!r.ok) return;
        const data = await r.json();
        // First time we see a run_id while running — lock onto it and reset.
        if (currentRunId === null && data.running) {
          currentRunId = data.run_id;
          knownTotal = 0;
          setProgressEvents([]);
        }
        // If the run_id changed mid-poll (new run started), reset.
        if (currentRunId !== null && data.run_id !== currentRunId) {
          currentRunId = data.run_id;
          knownTotal = 0;
          setProgressEvents([]);
        }
        // Only adopt events once we've locked onto the new run.
        if (currentRunId !== null && data.events && data.events.length > 0) {
          setProgressEvents((prev) => [...prev, ...data.events]);
          knownTotal = data.total;
        }
      } catch { /* swallow transient poll errors */ }
    };
    progressPollRef.current = setInterval(poll, 1500);
    // Small delay so the backend's _progress.reset() has fired before we poll.
    setTimeout(poll, 300);

    try {
      const res = await fetch(`${API}/api/scrape`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...(willScrapeAll ? {} : { ids: selectedIds }),
          ...(forceRefresh ? { force: true } : {}),
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setResults(data);
        if (data.cancelled) {
          const done = (data.bank_count || 0) - (data.cancelled_count || 0);
          setMessage(`⏹️ Fetch cancelled. ${done} of ${data.bank_count} banks completed before stop.`);
        } else {
          setMessage(`Fetch complete! ${data.bank_count} banks processed.`);
        }
      } else {
        setMessage(data.error || 'Fetch failed');
      }
    } catch {
      // The HTTP request was dropped (e.g., dev proxy disconnect on long
      // scrapes), but the backend is likely still running. Wait for the
      // server to mark the run as done, then fetch the saved latest result.
      setMessage('Connection dropped during fetch. Waiting for backend to finish...');
      const completed = await waitForBackendIdle();
      if (completed) {
        try {
          const r2 = await fetch(`${API}/api/results/latest`);
          if (r2.ok) {
            const data2 = await r2.json();
            setResults(data2);
            setMessage(`Fetch complete! ${data2.bank_count} banks processed.`);
          } else {
            setMessage('Fetch finished but no results were saved.');
          }
        } catch {
          setMessage('Fetch finished but failed to load results.');
        }
      } else {
        setMessage('Fetch did not finish within the wait window. Check the activity log.');
      }
    }
    finally {
      // Final drain of events
      await poll();
      if (progressPollRef.current) {
        clearInterval(progressPollRef.current);
        progressPollRef.current = null;
      }
      setScraping(false);
      setStopping(false);
    }
  };

  // Ask the backend to cancel the in-flight scrape. Workers will skip any
  // remaining banks and the current bank's agent run is asked to cancel.
  const stopScrape = async () => {
    if (!scraping || stopping) return;
    setStopping(true);
    setMessage('Stopping fetch — waiting for the current bank to wind down...');
    try {
      await fetch(`${API}/api/scrape/cancel`, { method: 'POST' });
    } catch {
      // Backend dropped the request; the run might still wind down on its
      // own. Don't unset stopping — the scrape's finally{} block will.
    }
  };

  const exportExcel = async () => {
    setExporting(true);
    setMessage('Generating Excel...');
    try {
      const res = await fetch(`${API}/api/export-excel`, { method: 'POST' });
      const data = await res.json();
      if (res.ok) setMessage(`Excel exported: ${data.blob_name}`);
      else setMessage(data.error || 'Export failed');
    } catch { setMessage('Export request failed'); }
    finally { setExporting(false); }
  };

  const resetScreen = async () => {
    setResults(null);
    setProgressEvents([]);
    setMessage('Screen reset — results cleared. URLs retained.');
    try {
      await fetch(`${API}/api/results/latest`, { method: 'DELETE' });
    } catch { /* best-effort: state is already cleared */ }
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>🏦 FD Rate Aggregator</h1>
        <p className="subtitle">Indian Bank Fixed Deposit Rate Aggregator</p>
      </header>

      {message && <div className="message-bar">{message}<button onClick={() => setMessage('')}>✕</button></div>}

      <div className="main-layout">
        <aside className="sidebar">
          <UrlManager
            urls={urls}
            onAdd={addUrl}
            onDelete={deleteUrl}
            selectedIds={selectedIds}
            onToggle={(id) =>
              setSelectedIds((prev) =>
                prev.map(String).includes(String(id))
                  ? prev.filter((x) => String(x) !== String(id))
                  : [...prev, id]
              )
            }
            onSelectAll={() => setSelectedIds(urls.map((u) => u.id))}
            onSelectNone={() => setSelectedIds([])}
          />
          <div className="action-buttons">
            <ScrapeButton
              onClick={scrapeAll}
              loading={scraping}
              disabled={urls.length === 0}
              selectedCount={selectedIds.length}
              totalCount={urls.length}
            />
            <button
              className="btn btn-stop"
              onClick={stopScrape}
              disabled={!scraping || stopping}
              title="Cancel the current fetch run — finishes the in-flight bank's API call as quickly as possible and skips the rest."
              style={{
                background: stopping ? '#9ca3af' : '#dc2626',
                color: 'white',
                border: 'none',
                padding: '8px 12px',
                borderRadius: 6,
                cursor: scraping && !stopping ? 'pointer' : 'not-allowed',
                opacity: scraping ? 1 : 0.5,
              }}
            >
              {stopping ? '⏳ Stopping...' : '⏹️ Stop Fetch'}
            </button>
            <label
              className="force-refresh-toggle"
              title="Bypass the HTTP cache and force a full re-scrape of every selected URL, even if the bank's page hasn't changed since the last run."
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                fontSize: '0.85rem', padding: '4px 0', cursor: 'pointer',
              }}
            >
              <input
                type="checkbox"
                checked={forceRefresh}
                onChange={(e) => setForceRefresh(e.target.checked)}
                disabled={scraping}
              />
              <span>Force refresh (skip cache)</span>
            </label>
            <ExportButton onClick={exportExcel} loading={exporting} disabled={!results} />
            <button
              className="btn btn-reset"
              onClick={resetScreen}
              disabled={!results && !message}
              title="Clear results and token usage from the screen (URLs are kept)"
            >
              🔄 Reset Screen
            </button>
          </div>
        </aside>
        <main className="content">
          <ProgressLog
            events={progressEvents}
            active={scraping}
            onDone={() => setProgressEvents([])}
          />
          <ResultsDashboard results={results} />
        </main>
      </div>
    </div>
  );
}

export default App;
