import React, { useState, useMemo } from 'react';

function UrlManager({ urls, onAdd, onDelete, selectedIds, onToggle, onSelectAll, onSelectNone }) {
  const [url, setUrl] = useState('');
  const [bankName, setBankName] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    if (url.trim() && bankName.trim()) {
      onAdd(url.trim(), bankName.trim());
      setUrl('');
      setBankName('');
    }
  };

  const selectedSet = useMemo(
    () => new Set((selectedIds || []).map(String)),
    [selectedIds]
  );
  const allSelected = urls.length > 0 && selectedSet.size === urls.length;
  const noneSelected = selectedSet.size === 0;

  return (
    <div className="card">
      <h2>📋 Bank URLs</h2>
      <form className="url-form" onSubmit={handleSubmit}>
        <input
          type="text"
          placeholder="Bank name (e.g., SBI)"
          value={bankName}
          onChange={(e) => setBankName(e.target.value)}
          required
        />
        <input
          type="url"
          placeholder="FD rate page URL"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required
        />
        <button type="submit" className="btn btn-primary">Add Bank URL</button>
      </form>

      {urls.length > 0 && (
        <>
          <div className="url-select-bar">
            <span className="url-select-count">
              {noneSelected
                ? `All ${urls.length} will be fetched`
                : `${selectedSet.size} of ${urls.length} selected`}
            </span>
            <div className="url-select-actions">
              <button
                type="button"
                className="link-btn"
                onClick={onSelectAll}
                disabled={allSelected}
              >
                Select all
              </button>
              <span className="sep">·</span>
              <button
                type="button"
                className="link-btn"
                onClick={onSelectNone}
                disabled={noneSelected}
              >
                Clear
              </button>
            </div>
          </div>
          <ul className="url-list">
            {urls.map((item) => {
              const checked = selectedSet.has(String(item.id));
              return (
                <li
                  key={item.id}
                  className={`url-item ${checked ? 'url-item-selected' : ''}`}
                >
                  <label className="url-checkbox" title="Include this URL in the next fetch">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggle && onToggle(item.id)}
                    />
                  </label>
                  <div className="url-text">
                    <div className="bank-name">{item.bank_name}</div>
                    <div className="bank-url">{item.url}</div>
                  </div>
                  <button className="btn btn-danger" onClick={() => onDelete(item.id)}>
                    Delete
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      )}

      {urls.length === 0 && (
        <p style={{ textAlign: 'center', color: '#94a3b8', marginTop: '1rem', fontSize: '0.85rem' }}>
          No URLs added yet. Add a bank URL above to get started.
        </p>
      )}
    </div>
  );
}

export default UrlManager;
