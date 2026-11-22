export async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

export function rebindReverseIndex(oldStoreId) {
  const q = oldStoreId ? `?old_store_id=${encodeURIComponent(oldStoreId)}` : "";
  return api(`/api/rebind/reverse-index${q}`);
}

export function rebindReverseVerify() {
  return api("/api/rebind/reverse-index/verify");
}

export function rebindRoute(storeId) {
  return api(`/api/rebind/route?store_id=${encodeURIComponent(storeId)}`);
}
