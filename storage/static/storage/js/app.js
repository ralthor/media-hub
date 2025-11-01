(function (window) {
  const tokenMeta = document.querySelector('meta[name="csrf-token"]');
  const value = tokenMeta && tokenMeta.getAttribute('content');
  const csrfToken = value && value !== 'NOTPROVIDED' ? value : '';

  const fetchWithCsrf = function (input, init) {
    const options = Object.assign({}, init);
    options.headers = new Headers((init && init.headers) || {});
    if (csrfToken && !options.headers.has('X-CSRFToken')) {
      options.headers.set('X-CSRFToken', csrfToken);
    }
    return fetch(input, options);
  };

  window.StorageApp = Object.assign({}, window.StorageApp, {
    csrfToken,
    fetchWithCsrf,
  });

  // Provide a global alias for convenience during the transition to AJAX.
  window.fetchWithCsrf = fetchWithCsrf;
})(window);
