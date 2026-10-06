import { useEffect, useState } from "react";
import { ExternalLink, LoaderCircle, RefreshCw } from "lucide-react";
import { request } from "./api";
import { useRead } from "./queries";

type Connection = {id: string; kind: string; public_url: string | null; enabled: boolean; browser_session?: boolean};

export default function DesktopNative({accountId, projectId, kind}: {accountId: string; projectId: string; kind: "forge" | "zulip"}) {
  const connection = useRead<{items: Connection[]}>(accountId, "integrations", projectId, `/projects/${encodeURIComponent(projectId)}/integrations`, !!projectId);
  const [attempt, setAttempt] = useState(0);
  const [session, setSession] = useState<{key: string; url?: string; error?: string} | null>(null);
  const item = connection.data?.items.find(candidate => candidate.kind === kind && candidate.enabled);
  const title = kind === "forge" ? "Forge" : "Zulip";
  const sessionKey = `${accountId}:${projectId}:${item?.id}:${attempt}`;
  useEffect(() => {
    if (!item?.public_url || !item.browser_session) return;
    const controller = new AbortController();
    setSession({key: sessionKey});
    void request<{url: string}>(`/projects/${encodeURIComponent(projectId)}/integrations/${encodeURIComponent(item.id)}/web-session`,
      {method: "POST", body: "{}", signal: controller.signal})
      .then(value => {if (!controller.signal.aborted) setSession({key: sessionKey, url: value.url});})
      .catch(error => {if (!controller.signal.aborted) setSession({key: sessionKey, error: error instanceof Error ? error.message : String(error)});});
    return () => controller.abort();
  }, [sessionKey, item?.id, item?.public_url, item?.browser_session, projectId]);
  const currentSession = session?.key === sessionKey ? session : null;
  const source = item?.browser_session ? currentSession?.url : item?.public_url;
  return <section aria-label={title}>
    {item?.public_url ? <><div className="desktop-native-toolbar"><strong>{title}</strong><a className="platform-icon-button" title={`Open ${title} in a new tab`} aria-label={`Open ${title} in a new tab`} href={source || item.public_url} target="_blank" rel="noopener noreferrer"><ExternalLink size={15}/></a><button className="platform-icon-button" title={`Reconnect ${title}`} aria-label={`Reconnect ${title}`} onClick={() => setAttempt(value => value + 1)}><RefreshCw size={15}/></button></div>
      {source ? <iframe key={sessionKey} title={title} src={source} className="desktop-native-frame" allow="clipboard-write"/> : <div className="desktop-native-error">{currentSession?.error ? <div className="platform-error" role="alert">{currentSession.error}<button className="platform-small-button" onClick={() => setAttempt(value => value + 1)}><RefreshCw size={14}/> Retry sign-in</button></div> : <div className="desktop-native-connecting" role="status"><LoaderCircle size={18}/> Signing in to {title}...</div>}</div>}</> : <div className="desktop-native-error">{connection.error ? <div className="platform-error" role="alert">{connection.error.message}</div> : <div className="platform-empty">{connection.isLoading ? `Connecting to ${title}...` : `${title} is not configured for this project.`}</div>}</div>}
  </section>;
}
