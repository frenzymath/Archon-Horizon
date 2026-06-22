// The transcript path string must match the Python endpoint registry exactly
// (it is what gets sha256-hashed in static mode), so ref is interpolated raw.
const transcriptPath = (ref: string) => `/api/transcript?ref=${ref}`;

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`fetch ${url} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export const getState = () => getJson<any>('/api/state');
export const getTranscripts = () => getJson<any[]>('/api/transcripts');
export const getTranscript = (ref: string) => getJson<any[]>(transcriptPath(ref));

export async function editInbox(payload: Record<string, unknown>): Promise<any> {
  const res = await fetch('/api/inbox', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return res.json();
}
