import { useEffect, useRef, useState } from 'react';
import { searchProducts, ApiError, type ProductSearchResult } from '../api/client';

// Search-as-you-type picker over the shared QTcal product catalog (same
// /api/products search endpoint the main app's quote line items use).
// Debounced so we don't fire a request on every keystroke; loads an initial
// unfiltered page immediately on open so the modal never opens empty.
const DEBOUNCE_MS = 300;

export default function ProductPickerModal({
  onClose,
  onSelect,
}: {
  onClose: () => void;
  onSelect: (product: ProductSearchResult) => void;
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<ProductSearchResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setError(null);
      searchProducts(query)
        .then(setResults)
        .catch((e) => setError(e instanceof ApiError ? e.message : 'Search failed'));
    }, DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query]);

  return (
    <div className="avrd-modal-overlay" onClick={onClose}>
      <div className="avrd-modal" style={{ width: 520 }} onClick={(e) => e.stopPropagation()}>
        <h3>Map to Product</h3>
        <div className="avrd-field">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by brand, model, SKU…"
            autoFocus
          />
        </div>

        {error && <div className="avrd-error">{error}</div>}

        <div style={{ maxHeight: 360, overflowY: 'auto' }}>
          {results === null && !error && (
            <p style={{ color: 'var(--gray-500)', fontSize: 13 }}>Searching…</p>
          )}
          {results && results.length === 0 && (
            <p style={{ color: 'var(--gray-500)', fontSize: 13 }}>
              No products found. Try a different search, or add it to your Products catalog first.
            </p>
          )}
          {(results || []).map((p) => (
            <div
              key={p.id}
              className="avrd-card"
              style={{ marginBottom: 8, padding: '10px 14px' }}
              onClick={() => onSelect(p)}
            >
              <div className="avrd-card-title" style={{ fontSize: 14 }}>
                {p.brand} {p.model}
              </div>
              <div className="avrd-card-sub">
                {p.description || p.category || 'No description'}
                {p.default_cost ? ` · ${p.cost_currency} ${p.default_cost.toLocaleString()}` : ''}
              </div>
            </div>
          ))}
        </div>

        <div className="avrd-modal-actions">
          <button type="button" className="avrd-btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
