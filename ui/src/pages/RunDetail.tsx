import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, modelLabel, streamRunEvents, type RunDetail as RunDetailType, type RunEvent } from "../api";

type StreamState = "connecting" | "live" | "closed" | "error";

const card: React.CSSProperties = {
  background: "#161b22",
  border: "1px solid #30363d",
  borderRadius: "8px",
  padding: "20px",
  marginBottom: "16px",
};

// Colour palette for transcript event types.
const eventStyles: Record<string, React.CSSProperties> = {
  // Lifecycle events
  queued: { color: "#8b949e" },
  in_progress: { color: "#58a6ff" },
  completed: { color: "#3fb950" },
  // Agent transcript events (new)
  agent_message: { color: "#e6edf3" },
  agent_thinking: { color: "#6e7681", fontStyle: "italic" },
  agent_tool_use: { color: "#d2a8ff" },
  agent_tool_result: { color: "#79c0ff" },
  agent_result: { color: "#3fb950", fontWeight: 600 },
  // Fallback
  default: { color: "#8b949e" },
};

// ---------------------------------------------------------------------------
// Display threshold for the expand/collapse toggle.
// Content longer than this (in characters) is collapsed by default; shorter
// content renders inline with no toggle.
// ---------------------------------------------------------------------------
const EXPAND_THRESHOLD_CHARS = 600;

// Number of characters shown in the collapsed preview.
const PREVIEW_CHARS = 300;

// Marker appended by the backend when a payload exceeded _MAX_TEXT_BYTES (32 KiB).
const TRUNCATION_MARKER = "…[truncated]";

// ---------------------------------------------------------------------------
// ExpandableContent — per-event collapse/expand control
// ---------------------------------------------------------------------------

interface ExpandableContentProps {
  /** Full stored text, potentially ending with TRUNCATION_MARKER. */
  text: string;
  style?: React.CSSProperties;
}

/** Renders long content collapsed behind a toggle.
 *
 * - Content ≤ EXPAND_THRESHOLD_CHARS renders inline with no toggle.
 * - Longer content shows a PREVIEW_CHARS-char preview + expand button.
 * - Toggle state is local to this instance; each event expands independently.
 * - Uses button semantics + aria-expanded for accessibility.
 * - If the stored text ends with the TRUNCATION_MARKER (backend 32 KiB cap
 *   was hit) a note is appended when expanded so the user knows the content
 *   was hard-truncated server-side.
 */
function ExpandableContent({ text, style }: ExpandableContentProps) {
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();

  const isLong = text.length > EXPAND_THRESHOLD_CHARS;
  const isTruncated = text.endsWith(TRUNCATION_MARKER);

  if (!isLong) {
    return (
      <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", ...style }}>
        {text}
      </span>
    );
  }

  const preview = text.slice(0, PREVIEW_CHARS);

  return (
    <span style={style}>
      {expanded ? (
        <>
          <span
            id={panelId}
            style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}
          >
            {text}
          </span>
          {isTruncated && (
            <span
              style={{
                display: "block",
                marginTop: "4px",
                color: "#8b949e",
                fontStyle: "italic",
                fontSize: "11px",
              }}
            >
              (content exceeds 32 KB — truncated by server)
            </span>
          )}
        </>
      ) : (
        <span
          id={panelId}
          style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}
        >
          {preview}
          <span style={{ color: "#6e7681" }}>…</span>
        </span>
      )}
      {" "}
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setExpanded((e) => !e)}
        style={{
          background: "none",
          border: "1px solid #30363d",
          borderRadius: "4px",
          color: "#58a6ff",
          cursor: "pointer",
          fontSize: "11px",
          padding: "1px 6px",
          verticalAlign: "middle",
          marginLeft: "4px",
          lineHeight: "1.4",
        }}
      >
        <span aria-hidden="true">{expanded ? "▴" : "▾"}</span>
        {" "}
        {expanded ? "collapse" : "expand"}
      </button>
    </span>
  );
}

/** Render a single event row, with richer layout for transcript event types. */
function EventRow({ ev }: { ev: RunEvent }) {
  const style = eventStyles[ev.event_type] ?? eventStyles.default;
  const time = new Date(ev.timestamp).toLocaleTimeString();

  // agent_message — show prose text (expandable when long)
  if (ev.event_type === "agent_message") {
    return (
      <div style={{ marginBottom: "8px", ...style }}>
        <span style={{ color: "#6e7681", marginRight: "8px" }}>[{time}]</span>
        <ExpandableContent text={String(ev.data.text ?? "")} />
      </div>
    );
  }

  // agent_thinking — collapsible italics block (expandable when long)
  if (ev.event_type === "agent_thinking") {
    return (
      <div style={{ marginBottom: "6px", ...style }}>
        <span style={{ color: "#6e7681", marginRight: "8px" }}>[{time}]</span>
        <span style={{ color: "#6e7681" }}>💭 </span>
        <ExpandableContent text={String(ev.data.thinking ?? "")} />
      </div>
    );
  }

  // agent_tool_use — monospace name + input summary (expandable when long)
  if (ev.event_type === "agent_tool_use") {
    return (
      <div style={{ marginBottom: "6px", ...style }}>
        <span style={{ color: "#6e7681", marginRight: "8px" }}>[{time}]</span>
        <span style={{ fontWeight: 700 }}>⚙ {String(ev.data.name ?? "")}</span>
        {ev.data.input_summary ? (
          <span
            style={{
              color: "#8b949e",
              marginLeft: "8px",
              fontFamily: "monospace",
              fontSize: "12px",
            }}
          >
            <ExpandableContent
              text={String(ev.data.input_summary)}
              style={{ fontFamily: "monospace", fontSize: "12px" }}
            />
          </span>
        ) : null}
      </div>
    );
  }

  // agent_tool_result — monospace content (expandable when long)
  if (ev.event_type === "agent_tool_result") {
    return (
      <div style={{ marginBottom: "6px", ...style }}>
        <span style={{ color: "#6e7681", marginRight: "8px" }}>[{time}]</span>
        <span style={{ color: "#6e7681", marginRight: "4px" }}>↩</span>
        <ExpandableContent
          text={String(ev.data.content ?? "")}
          style={{ fontFamily: "monospace", fontSize: "12px" }}
        />
      </div>
    );
  }

  // agent_result — bold final outcome (expandable when long)
  if (ev.event_type === "agent_result") {
    return (
      <div style={{ marginBottom: "8px", ...style }}>
        <span style={{ color: "#6e7681", marginRight: "8px" }}>[{time}]</span>
        <span style={{ marginRight: "6px" }}>✓ RESULT</span>
        {ev.data.subtype ? (
          <span style={{ color: "#58a6ff", marginRight: "6px" }}>
            [{String(ev.data.subtype)}]
          </span>
        ) : null}
        <ExpandableContent text={String(ev.data.result ?? "")} />
      </div>
    );
  }

  // Generic fallback for lifecycle / infrastructure events
  return (
    <div style={{ marginBottom: "6px", ...style }}>
      <span style={{ color: "#6e7681", marginRight: "8px" }}>[{time}]</span>
      <span style={{ fontWeight: 600 }}>{ev.event_type}</span>
      {Object.keys(ev.data).length > 0 && (
        <span style={{ color: "#8b949e", marginLeft: "8px", fontFamily: "monospace", fontSize: "12px" }}>
          {JSON.stringify(ev.data)}
        </span>
      )}
    </div>
  );
}

export default function RunDetail() {
  const { run_id } = useParams<{ run_id: string }>();
  const [detail, setDetail] = useState<RunDetailType | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [dispatching, setDispatching] = useState(false);
  const eventsEndRef = useRef<HTMLDivElement>(null);
  const unsubRef = useRef<(() => void) | null>(null);

  // Fetch run metadata on mount / runId change.
  useEffect(() => {
    if (!run_id) return;
    api
      .getRun(run_id)
      .then((d) => {
        setDetail(d);
        // The SSE stream backfills all prior events, so we do NOT seed events
        // from the REST snapshot here — that would cause duplicates once the
        // stream delivers the same events. The SSE effect below owns event state.
      })
      .catch((e: Error) => setError(e.message));
  }, [run_id]);

  // Auto-connect the SSE stream on mount; reconnect whenever runId changes.
  // The backend backfills all prior events then streams live ones — so a single
  // connection gives the full transcript from the start without a separate REST
  // seed. On reconnect we clear accumulated events so the backfill replay is
  // the sole source of truth and there are no duplicates.
  const connect = useCallback(() => {
    if (!run_id) return;

    // Tear down any existing connection before opening a new one.
    unsubRef.current?.();
    unsubRef.current = null;

    setEvents([]);
    setStreamState("connecting");

    const unsub = streamRunEvents(
      run_id,
      (ev) => {
        setStreamState("live");
        setEvents((prev) => [...prev, ev]);
      },
      (err) => {
        // Distinguish a clean close (undefined / null) from a real error.
        if (err != null) {
          setStreamState("error");
        } else {
          setStreamState("closed");
        }
      },
    );
    unsubRef.current = unsub;
  }, [run_id]);

  // Auto-connect on mount and when runId changes; clean up on unmount.
  useEffect(() => {
    connect();
    return () => {
      unsubRef.current?.();
      unsubRef.current = null;
    };
  }, [connect]);

  // Keep the transcript scrolled to the bottom as events arrive.
  useEffect(() => {
    eventsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

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

  // Stream status pill shown in the transcript header.
  const streamBadge = (() => {
    switch (streamState) {
      case "connecting":
        return (
          <span style={{ color: "#8b949e", fontSize: "13px" }}>
            Connecting…
          </span>
        );
      case "live":
        return (
          <span style={{ color: "#3fb950", fontSize: "13px" }}>
            ● Live
          </span>
        );
      case "closed":
        return (
          <span style={{ color: "#8b949e", fontSize: "13px" }}>
            Stream closed —{" "}
            <button
              onClick={connect}
              style={{
                background: "none",
                border: "none",
                color: "#58a6ff",
                cursor: "pointer",
                fontSize: "13px",
                padding: 0,
                textDecoration: "underline",
              }}
            >
              Reconnect
            </button>
          </span>
        );
      case "error":
        return (
          <span style={{ color: "#f85149", fontSize: "13px" }}>
            Stream error —{" "}
            <button
              onClick={connect}
              style={{
                background: "none",
                border: "none",
                color: "#58a6ff",
                cursor: "pointer",
                fontSize: "13px",
                padding: 0,
                textDecoration: "underline",
              }}
            >
              Reconnect
            </button>
          </span>
        );
    }
  })();

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
            <div>
              <span style={{ color: "#8b949e" }}>Status: </span>
              <span style={{
                color: detail.status === "awaiting_quota" ? "#d29922" : "inherit",
                fontWeight: detail.status === "awaiting_quota" ? 600 : "inherit",
              }}>
                {detail.status === "awaiting_quota" ? "quota: waiting" : detail.status}
              </span>
              {detail.status === "awaiting_quota" && detail.quota_reset_at && (
                <span style={{ color: "#8b949e", fontSize: "12px", marginLeft: "8px" }}>
                  (retries at {new Date(detail.quota_reset_at).toLocaleString()})
                </span>
              )}
            </div>
            <div><span style={{ color: "#8b949e" }}>Type: </span>{detail.type}</div>
            {modelLabel(detail.model) && (
              <div><span style={{ color: "#8b949e" }}>Model: </span>{modelLabel(detail.model)}</div>
            )}
            <div><span style={{ color: "#8b949e" }}>Repo: </span>{detail.repo.owner}/{detail.repo.name}</div>
            <div><span style={{ color: "#8b949e" }}>Started: </span>{new Date(detail.started_at).toLocaleString()}</div>
            {detail.build_status && (
              <div>
                <span style={{ color: "#8b949e" }}>Build: </span>
                <span style={{
                  display: "inline-block",
                  padding: "2px 8px",
                  borderRadius: "12px",
                  fontSize: "12px",
                  fontWeight: 600,
                  background:
                    detail.build_status === "BUILDING" ? "#7d4e00" :
                    detail.build_status === "CONVERGING" ? "#0d419d" :
                    detail.build_status === "APPROVED" ? "#1a4f1a" :
                    "#21262d",
                  color:
                    detail.build_status === "BUILDING" ? "#e3b341" :
                    detail.build_status === "CONVERGING" ? "#58a6ff" :
                    detail.build_status === "APPROVED" ? "#3fb950" :
                    "#8b949e",
                }}>
                  {detail.build_status}
                </span>
              </div>
            )}
            {detail.pr_ref && (
              <div>
                <span style={{ color: "#8b949e" }}>Draft PR: </span>
                <span>#{detail.pr_ref.number}</span>
              </div>
            )}
            {detail.issue_ref && (
              <div>
                <span style={{ color: "#8b949e" }}>Issue: </span>
                <span>#{detail.issue_ref.number}</span>
              </div>
            )}
            {detail.changed_files != null && (
              <div><span style={{ color: "#8b949e" }}>Changed files: </span>{detail.changed_files}</div>
            )}
          </div>
        </div>
      )}

      <div style={card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
          <h2 style={{ fontSize: "16px" }}>Run Transcript</h2>
          {streamBadge}
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
          {events.length === 0 && streamState === "connecting" && (
            <span style={{ color: "#6e7681" }}>Connecting to stream…</span>
          )}
          {events.length === 0 && streamState === "live" && (
            <span style={{ color: "#6e7681" }}>Waiting for events…</span>
          )}
          {events.length === 0 && (streamState === "closed" || streamState === "error") && (
            <span style={{ color: "#6e7681" }}>No events received.</span>
          )}
          {events.map((ev, i) => (
            <EventRow key={i} ev={ev} />
          ))}
          <div ref={eventsEndRef} />
        </div>
      </div>
    </div>
  );
}
