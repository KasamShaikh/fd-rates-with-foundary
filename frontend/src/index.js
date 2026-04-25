// React entry point.
// Mounts the top-level <App /> component into the #root element defined in
// public/index.html. StrictMode is enabled to surface common React pitfalls
// (e.g., unsafe lifecycles, side-effects in render) during development.
import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<React.StrictMode><App /></React.StrictMode>);
