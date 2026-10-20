const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
// CSRF token is short-lived and held in memory only. The actual login token
// is an HttpOnly, SameSite=Strict cookie set by the control plane.
let csrfToken: string | null = null;
export function setCsrfToken(value: string | null): void {
  csrfToken = value;
}

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export interface RequestOptions extends RequestInit {
  /** Cooperative abort after this many milliseconds. */
  timeoutMs?: number;
}

async function request<T>(path: string, init: RequestOptions = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (csrfToken && init.method && !["GET", "HEAD"].includes(init.method)) {
    headers.set("X-CSRF-Token", csrfToken);
  }

  const { timeoutMs, ...fetchInit } = init;
  const controller = timeoutMs ? new AbortController() : null;
  const timer = controller
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : null;

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...fetchInit,
      credentials: "same-origin",
      headers,
      signal: controller?.signal,
    });
  } catch (error) {
    throw new ApiError(
      0,
      "NETWORK_ERROR",
      "Cannot reach the control plane. Is `jev serve` running?",
      String(error),
    );
  } finally {
    if (timer !== null) window.clearTimeout(timer);
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    const contract = payload as
      | { error?: { code?: string; message?: string; details?: unknown } }
      | null;
    throw new ApiError(
      response.status,
      contract?.error?.code ?? `HTTP_${response.status}`,
      contract?.error?.message ?? `Request failed (${response.status})`,
      contract?.error?.details,
    );
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, {
      ...options,
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "PUT",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
};

export function eventStreamUrl(): string {
  const root = BASE
    ? BASE.replace(/^http/, "ws")
    : `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
  return `${root}/ws/events`;
}
