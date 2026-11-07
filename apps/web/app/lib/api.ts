export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { cache: "no-store", ...init });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body ? `Request failed: ${response.status} ${body}` : `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}
