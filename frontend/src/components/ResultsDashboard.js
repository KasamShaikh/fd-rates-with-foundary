import React, { useState, useMemo } from 'react';

function ResultsDashboard({ results }) {
  const [expandedBanks, setExpandedBanks] = useState({});
  const [categoryFilter, setCategoryFilter] = useState('All');

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

  const toggleBank = (idx) => {
    setExpandedBanks((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  if (!results) {
    return (
      <div className="card results-empty">
        <h2>📈 Results Dashboard</h2>
        <p>No results yet. Add bank URLs and run a scrape to see FD rates here.</p>
      </div>
    );
  }

  const tokenUsage = results?.token_usage || null;

  return (
    <div className="card">
      <h2>📈 Results Dashboard</h2>
      <div className="meta-info">
        Scraped at: {results.scraped_at} | Banks: {results.bank_count}
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
    </div>
  );
}

export default ResultsDashboard;
