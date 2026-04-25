import React, { useState, useMemo } from 'react';

function ResultsDashboard({ results }) {
  const [expandedBanks, setExpandedBanks] = useState({});
  const [categoryFilter, setCategoryFilter] = useState('All');
  const [activeTab, setActiveTab] = useState('rates');

  const bankResults = results?.results || [];

  // Collect all unique category names across all banks
  const allCategories = useMemo(() => {
    const cats = new Set(['All']);
    bankResults.forEach((bank) => {
      (bank.categories || []).forEach((cat) => {
        if (cat.category_name) cats.add(cat.category_name);
      });
    });
    return Array.from(cats);
  }, [bankResults]);

  // Summary stats for the Summary tab
  const summary = useMemo(() => {
    const rows = bankResults.map((bank) => {
      const hasError = !!bank.error;
      const categories = bank.categories || [];
      const rateCount = categories.reduce(
        (sum, c) => sum + (c.rates ? c.rates.length : 0),
        0
      );
      return {
        bank_name: bank.bank_name || 'Unknown',
        url: bank.url || '',
        status: hasError ? 'Failed' : 'Success',
        categories: categories.length,
        rates: rateCount,
        effective_date: bank.effective_date || '—',
        error: bank.error || '',
        reason: bank.reason || '',
      };
    });
    const successCount = rows.filter((r) => r.status === 'Success').length;
    const failCount = rows.length - successCount;
    const totalRates = rows.reduce((s, r) => s + r.rates, 0);
    return { rows, successCount, failCount, totalRates };
  }, [bankResults]);

  const toggleBank = (idx) => {
    setExpandedBanks((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  if (!results) {
    return (
      <div className="card results-empty">
        <h2>📈 Results Dashboard</h2>
        <p>No results yet. Add bank URLs and run a fetch to see FD rates here.</p>
      </div>
    );
  }

  const tokenUsage = results?.token_usage || null;

  return (
    <div className="card">
      <h2>📈 Results Dashboard</h2>
      <div className="meta-info">
        Fetched at: {results.scraped_at} | Banks: {results.bank_count}
        {typeof results.di_pages === 'number' && ` | DI pages: ${results.di_pages}`}
        {typeof results.elapsed_seconds === 'number' && ` | Time: ${(() => {
          const total = Math.round(results.elapsed_seconds);
          const m = Math.floor(total / 60);
          const s = total % 60;
          return m > 0 ? `${m}m ${s}s` : `${s}s`;
        })()}`}
      </div>

      {tokenUsage && (
        <div className="token-usage-bar">
          <span className="token-label">🤖 Tokens used</span>
          <span className="token-stat"><strong>{(tokenUsage.total_tokens || 0).toLocaleString()}</strong> total</span>
          <span className="token-sep">·</span>
          <span className="token-stat">{(tokenUsage.prompt_tokens || 0).toLocaleString()} prompt</span>
          <span className="token-sep">·</span>
          <span className="token-stat">{(tokenUsage.completion_tokens || 0).toLocaleString()} completion</span>
        </div>
      )}

      {/* Tab switcher */}
      <div className="tab-bar">
        <button
          className={`tab ${activeTab === 'rates' ? 'active' : ''}`}
          onClick={() => setActiveTab('rates')}
        >
          📊 Rates
        </button>
        <button
          className={`tab ${activeTab === 'summary' ? 'active' : ''}`}
          onClick={() => setActiveTab('summary')}
        >
          📋 Summary
          <span className="tab-badge">{summary.successCount}/{summary.rows.length}</span>
        </button>
      </div>

      {activeTab === 'summary' ? (
        <div className="summary-pane">
          <div className="summary-cards">
            <div className="summary-card summary-card-success">
              <div className="summary-card-label">Successful</div>
              <div className="summary-card-value">{summary.successCount}</div>
            </div>
            <div className="summary-card summary-card-fail">
              <div className="summary-card-label">Failed</div>
              <div className="summary-card-value">{summary.failCount}</div>
            </div>
            <div className="summary-card summary-card-rates">
              <div className="summary-card-label">Total Rates</div>
              <div className="summary-card-value">{summary.totalRates.toLocaleString()}</div>
            </div>
            <div className="summary-card summary-card-banks">
              <div className="summary-card-label">Banks</div>
              <div className="summary-card-value">{summary.rows.length}</div>
            </div>
          </div>

          <h3 className="summary-heading">Per-bank details</h3>
          <div className="summary-table-wrap">
            <table className="summary-table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Bank</th>
                  <th>Categories</th>
                  <th>Rates</th>
                  <th>Effective</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {summary.rows.map((row, i) => (
                  <tr key={i} className={row.status === 'Failed' ? 'row-fail' : 'row-ok'}>
                    <td>
                      <span className={`status-pill ${row.status === 'Failed' ? 'pill-fail' : 'pill-ok'}`}>
                        {row.status === 'Failed' ? '✖ Failed' : '✔ OK'}
                      </span>
                    </td>
                    <td>
                      <div style={{ fontWeight: 600 }}>{row.bank_name}</div>
                      {row.url && (
                        <a href={row.url} target="_blank" rel="noreferrer" className="summary-url">
                          {row.url}
                        </a>
                      )}
                    </td>
                    <td>{row.categories}</td>
                    <td>{row.rates}</td>
                    <td>{row.effective_date}</td>
                    <td className="summary-details">
                      {row.status === 'Failed'
                        ? (row.reason || row.error || 'Unknown error')
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <>
          {/* Category filters */}
          <div className="filters">
            {allCategories.map((cat) => (
              <span
                key={cat}
                className={`filter-chip ${categoryFilter === cat ? 'active' : ''}`}
                onClick={() => setCategoryFilter(cat)}
              >
                {cat}
              </span>
            ))}
          </div>

          {/* Bank-wise results */}
          {bankResults.map((bank, bankIdx) => {
            const isExpanded = expandedBanks[bankIdx] !== false; // default expanded
            const categories = (bank.categories || []).filter(
              (cat) => categoryFilter === 'All' || cat.category_name === categoryFilter
            );

            return (
              <div key={bankIdx} className="bank-section">
                <h3 onClick={() => toggleBank(bankIdx)}>
                  <span>
                    {bank.bank_name || 'Unknown Bank'}
                    {bank.effective_date && ` — Effective: ${bank.effective_date}`}
                  </span>
                  <span className="toggle">{isExpanded ? '▲' : '▼'}</span>
                </h3>

                {isExpanded && (
                  <div style={{ border: '1px solid #e2e8f0', borderTop: 'none', borderRadius: '0 0 6px 6px', overflow: 'auto' }}>
                    {bank.error ? (
                      <div style={{ padding: '1rem', color: '#dc2626' }}>
                        Error: {bank.error}<br />
                        {bank.reason && <span>Reason: {bank.reason}</span>}
                      </div>
                    ) : categories.length === 0 ? (
                      <div style={{ padding: '1rem', color: '#94a3b8' }}>
                        No matching categories found.
                      </div>
                    ) : (
                      categories.map((cat, catIdx) => (
                        <div key={catIdx} style={{ padding: '0.75rem' }}>
                          <div style={{ fontWeight: 600, fontSize: '0.9rem', marginBottom: '0.25rem' }}>
                            {cat.category_name}
                            {cat.amount_slab && ` — ${cat.amount_slab}`}
                            {cat.scheme_name && ` (${cat.scheme_name})`}
                          </div>
                          <table className="rate-table">
                            <thead>
                              <tr>
                                <th>Tenor</th>
                                <th>Min Days</th>
                                <th>Max Days</th>
                                <th>Rate (%)</th>
                                <th>Info</th>
                              </tr>
                            </thead>
                            <tbody>
                              {(cat.rates || []).map((rate, rIdx) => (
                                <tr key={rIdx}>
                                  <td>{rate.tenor_description || '—'}</td>
                                  <td>{rate.min_days ?? '—'}</td>
                                  <td>{rate.max_days ?? '—'}</td>
                                  <td style={{ fontWeight: 600 }}>{rate.rate_percent ?? '—'}</td>
                                  <td>{rate.additional_info || '—'}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ))
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}

export default ResultsDashboard;
