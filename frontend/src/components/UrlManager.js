import React, { useState } from 'react';

function UrlManager({ urls, onAdd, onDelete }) {
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
        <ul className="url-list">
          {urls.map((item) => (
            <li key={item.id} className="url-item">
              <div>
                <div className="bank-name">{item.bank_name}</div>
                <div className="bank-url">{item.url}</div>
              </div>
              <button className="btn btn-danger" onClick={() => onDelete(item.id)}>
                Delete
              </button>
            </li>
          ))}
        </ul>
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
