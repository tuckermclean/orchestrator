import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, streamRunEvents, type RunDetail as RunDetailType, type RunEvent } from "../api";

const card: React.CSSProperties = {
  background: "#161b22",
  border: "1px solid #30363d",
  borderRadius: "8px",
  padding: "20px",
  marginBottom: "16px",
};

const eventStyles: Record<string, React.CSSProperties> = {
  queued: { color: "#8b949e" },
  in_progress: { color: "#58a6ff" },
  tool_use: { color: "#d2a8ff" },
  completed: { color: "#3fb950" },
  default: { color: "#e6edf3" },
};

export default function RunDetail() {
  const { run_id } = useParams<{ run_id: string }>();
  const [detail, setDetail] = useState<RunDetailType | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dispatching, setDispatching] = useState(false);
  const eventsEndRef = useRef<HTMLDivElement>(null);
  const unsubRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (!run_id) return;
    api
      .getRun(run_id)
      .then((d) => {
        setDetail(d);
        setEvents(d.events);
      })
      .catch((e: Error) => setError(e.message));
  }, [run_id]);

  useEffect(() => {
    eventsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  const startStream = () => {
    if (!run_id || streaming) return;
    setStreaming(true);
    const unsub = streamRunEvents(
      run_id,
      (ev) => setEvents((prev) => [...prev, ev]),
      () => setStreaming(false),
    );
    unsubRef.current = unsub;
  };

  const stopStream = () => {
    unsubRef.current?.();
    unsubRef.current = null;
    setStreaming(false);
  };

  useEffect(() => () => unsubRef.current?.(), []);

  const handleDevDispatch = async () => {
    setDispatching(true);
    try {
      const result = await api.devDispatch();
      // Navigate to the new run
      window.location.href = `/runs/${result.run_id}`;
    } catch (e) {
      setError(String(e));
    } finally {
      setDispatching(false);
    }
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
        <h1 style={{ fontSize: "24px" }}>Run: {run_id}</h1>
        <button
          onClick={handleDevDispatch}
          disabled={dispatching}
          style={{
            background: "#238636",
            color: "#fff",
            border: "none",
            borderRadius: "6px",
            padding: "8px 18px",
            cursor: "pointer",
            fontSize: "14px",
            fontWeight: 600,
          }}
        >
          {dispatching ? "Dispatching…" : "Dispatch New Run"}
        </button>
      </div>

      {error && (
        <div style={{ ...card, borderColor: "#f85149", color: "#f85149" }}>
          Error: {error}
        </div>
      )}

      {detail && (
        <div style={card}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px", fontSize: "14px" }}>
            <div><span style={{ color: "#8b949e" }}>Status: </span>{detail.status}</div>
            <div><span style={{ color: "#8b949e" }}>Type: </span>{detail.type}</div>
            <div><span style={{ color: "#8b949e" }}>Repo: </span>{detail.repo.owner}/{detail.repo.name}</div>
            <div><span style={{ color: "#8b949e" }}>Started: </span>{new Date(detail.started_at).toLocaleString()}</div>
          </div>
        </div>
      )}

      <div style={card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
          <h2 style={{ fontSize: "16px" }}>Live Event Stream</h2>
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              onClick={startStream}
              disabled={streaming}
              style={{
                background: streaming ? "#21262d" : "#1f6feb",
                color: "#fff",
                border: "none",
                borderRadius: "6px",
                padding: "6px 14px",
                cursor: streaming ? "not-allowed" : "pointer",
                fontSize: "13px",
              }}
            >
              {streaming ? "● Live" : "Connect SSE"}
            </button>
            {streaming && (
              <button
                onClick={stopStream}
                style={{
                  background: "#21262d",
                  color: "#e6edf3",
                  border: "1px solid #30363d",
                  borderRadius: "6px",
                  padding: "6px 14px",
                  cursor: "pointer",
                  fontSize: "13px",
                }}
              >
                Disconnect
              </button>
            )}
          </div>
        </div>

        <div
          style={{
            background: "#0d1117",
            border: "1px solid #21262d",
            borderRadius: "6px",
            padding: "12px",
            minHeight: "200px",
            maxHeight: "500px",
            overflowY: "auto",
            fontFamily: "monospace",
            fontSize: "13px",
          }}
        >
          {events.length === 0 && (
            <span style={{ color: "#6e7681" }}>No events yet. Connect SSE to stream live events.</span>
          )}
          {events.map((ev, i) => {
            const style = eventStyles[ev.event_type] ?? eventStyles.default;
            return (
              <div key={i} style={{ marginBottom: "6px", ...style }}>
                <span style={{ color: "#6e7681", marginRight: "8px" }}>
                  [{new Date(ev.timestamp).toLocaleTimeString()}]
                </span>
                <span style={{ fontWeight: 600 }}>{ev.event_type}</span>
                {Object.keys(ev.data).length > 0 && (
                  <span style={{ color: "#8b949e", marginLeft: "8px" }}>
                    {JSON.stringify(ev.data)}
                  </span>
                )}
              </div>
            );
          })}
          <div ref={eventsEndRef} />
        </div>
      </div>
    </div>
  );
}
