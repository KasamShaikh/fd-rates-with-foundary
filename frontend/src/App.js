import React, { useState, useEffect, useCallback } from 'react';
import UrlManager from './components/UrlManager';
import ScrapeButton from './components/ScrapeButton';
import ExportButton from './components/ExportButton';
import ResultsDashboard from './components/ResultsDashboard';
import './App.css';

const API = process.env.REACT_APP_API_BASE_URL || '';

function App() {
  const [urls, setUrls] = useState([]);
  const [results, setResults] = useState(null);
  const [scraping, setScraping] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [message, setMessage] = useState('');

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

  const scrapeAll = async () => {
    setScraping(true);
    setMessage('Scraping in progress... This may take a few minutes.');
    try {
      const res = await fetch(`${API}/api/scrape`, { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        setResults(data);
        setMessage(`Scrape complete! ${data.bank_count} banks processed.`);
      } else {
        setMessage(data.error || 'Scrape failed');
      }
    } catch { setMessage('Scrape request failed'); }
    finally { setScraping(false); }
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

  return (
    <div className="app">
      <header className="app-header">
        <h1>🏦 FD Rate Scraper</h1>
        <p className="subtitle">Indian Bank Fixed Deposit Rate Aggregator</p>
      </header>

      {message && <div className="message-bar">{message}<button onClick={() => setMessage('')}>✕</button></div>}

      <div className="main-layout">
        <aside className="sidebar">
          <UrlManager urls={urls} onAdd={addUrl} onDelete={deleteUrl} />
          <div className="action-buttons">
            <ScrapeButton onClick={scrapeAll} loading={scraping} disabled={urls.length === 0} />
            <ExportButton onClick={exportExcel} loading={exporting} disabled={!results} />
          </div>
        </aside>
        <main className="content">
          <ResultsDashboard results={results} />
        </main>
      </div>
    </div>
  );
}

export default App;
