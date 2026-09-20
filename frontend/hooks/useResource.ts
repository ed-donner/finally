"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface Resource<T> {
  data: T | null;
  loading: boolean; // true only until the first response (or failure) arrives
  error: string | null;
  refresh: () => Promise<void>;
}

/**
 * Fetch-on-mount with optional polling and a manual `refresh`. The last good `data` is kept
 * when a refresh fails, so a flaky backend does not blank the screen.
 */
export function useResource<T>(fetcher: () => Promise<T>, pollMs?: number): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;

  const refresh = useCallback(async () => {
    try {
      setData(await fetchRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    if (!pollMs) return;
    const id = setInterval(() => void refresh(), pollMs);
    return () => clearInterval(id);
  }, [refresh, pollMs]);

  return { data, loading, error, refresh };
}
