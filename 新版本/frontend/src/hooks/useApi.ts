import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../lib/api";

export function useApi<T>(path: string | null, intervalMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const pathRef = useRef(path);
  pathRef.current = path;

  const reload = useCallback(async () => {
    if (!pathRef.current) return;
    try {
      const result = await api.get<T>(pathRef.current);
      setData(result);
      setError(null);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught
          : new ApiError(0, "UNKNOWN", String(caught)),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(Boolean(path));
    if (!path) return;
    void reload();
    if (intervalMs <= 0) return;
    const timer = window.setInterval(() => void reload(), intervalMs);
    return () => window.clearInterval(timer);
  }, [path, intervalMs, reload]);

  return { data, error, loading, reload, setData };
}
