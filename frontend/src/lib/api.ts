// HTTP-клиент панели: токен в localStorage (обёрнуто в try/catch — хранилище может быть недоступно)
const TOKEN_KEY = "tradebot.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* приватный режим — токен живёт только в памяти вкладки */
  }
  memoryToken = token;
}

let memoryToken: string | null = null;
const onUnauthorized = new Set<() => void>();

export function subscribeUnauthorized(fn: () => void): () => void {
  onUnauthorized.add(fn);
  return () => onUnauthorized.delete(fn);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken() ?? memoryToken;
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const resp = await fetch(`/api${path}`, { ...init, headers });
  if (resp.status === 401 && path !== "/auth/login") {
    setToken(null);
    onUnauthorized.forEach((fn) => fn());
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* не JSON */
    }
    throw new ApiError(resp.status, detail);
  }
  const type = resp.headers.get("Content-Type") ?? "";
  return (type.includes("json") ? resp.json() : resp.text()) as Promise<T>;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });

export async function download(path: string, filename: string): Promise<void> {
  const token = getToken() ?? memoryToken;
  const resp = await fetch(`/api${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) throw new ApiError(resp.status, resp.statusText);
  const url = URL.createObjectURL(await resp.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
