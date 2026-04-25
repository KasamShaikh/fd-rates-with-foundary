import React, { useState, useEffect, useCallback, useRef } from 'react';
import UrlManager from './components/UrlManager';
import ScrapeButton from './components/ScrapeButton';
import ExportButton from './components/ExportButton';
import ResultsDashboard from './components/ResultsDashboard';
import ProgressLog from './components/ProgressLog';
import './App.css';

const API = process.env.REACT_APP_API_BASE_URL || '';

function App() {
  const [urls, setUrls] = useState([]);
  const [results, setResults] = useState(null);
  const [scraping, setScraping] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [message, setMessage] = useState('');
  const [progressEvents, setProgressEvents] = useState([]);
  const [selectedIds, setSelectedIds] = useState([]);
  const progressPollRef = useRef(null);

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
        ? `Scraping all ${urls.length} URL${urls.length === 1 ? '' : 's'}... watch the live activity below.`
        : `Scraping ${targetCount} selected URL${targetCount === 1 ? '' : 's'}... watch the live activity below.`
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
        body: JSON.stringify(willScrapeAll ? {} : { ids: selectedIds }),
      });
      const data = await res.json();
      if (res.ok) {
        setResults(data);
        setMessage(`Scrape complete! ${data.bank_count} banks processed.`);
      } else {
        setMessage(data.error || 'Scrape failed');
      }
    } catch {
      // The HTTP request was dropped (e.g., dev proxy disconnect on long
      // scrapes), but the backend is likely still running. Wait for the
      // server to mark the run as done, then fetch the saved latest result.
      setMessage('Connection dropped during scrape. Waiting for backend to finish...');
      const completed = await waitForBackendIdle();
      if (completed) {
        try {
          const r2 = await fetch(`${API}/api/results/latest`);
          if (r2.ok) {
            const data2 = await r2.json();
            setResults(data2);
            setMessage(`Scrape complete! ${data2.bank_count} banks processed.`);
          } else {
            setMessage('Scrape finished but no results were saved.');
          }
        } catch {
          setMessage('Scrape finished but failed to load results.');
        }
      } else {
        setMessage('Scrape did not finish within the wait window. Check the activity log.');
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
        <h1>🏦 FD Rate Scraper</h1>
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
