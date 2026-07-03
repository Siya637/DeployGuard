import React, { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

let _id = 0;
const nextId = () => ++_id;

export default function App() {
  const [messages, setMessages] = useState([]);
  // phase: idle | streaming | qa
  const [phase, setPhase] = useState("idle");
  const [input, setInput] = useState("");

  // Mutable run state kept in refs so the streaming loop always sees latest.
  const jobId = useRef(null);
  const currentAssistant = useRef(null); // id of the bubble being streamed into
  const questions = useRef([]);
  const qaIndex = useRef(0);
  const scrollRef = useRef(null);

  const busy = phase === "streaming";

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, phase]);

  // ---- message helpers ----------------------------------------------------
  function addMessage(msg) {
    const id = nextId();
    setMessages((prev) => [...prev, { id, stages: [], text: "", ...msg }]);
    return id;
  }

  function updateMessage(id, updater) {
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, ...updater(m) } : m))
    );
  }

  // ---- submit handler (repo URL OR an answer during Q&A) ------------------
  async function handleSubmit(e) {
    e?.preventDefault();
    const value = input.trim();
    if (!value) return;

    // Clear the input up front. startAnalysis() awaits the entire SSE stream,
    // so clearing *after* that await left the repo URL sitting in the box all
    // the way through the first question. Clearing here fixes that first
    // transition (and is harmless for the Q&A path, which already cleared).
    setInput("");

    if (phase === "qa") {
      await submitAnswer(value);
    } else if (phase === "idle") {
      await startAnalysis(value);
    }
  }

  async function startAnalysis(repoUrl) {
    addMessage({ role: "user", text: repoUrl });
    jobId.current = null;
    questions.current = [];
    qaIndex.current = 0;
    currentAssistant.current = addMessage({ role: "assistant", streaming: true });
    setPhase("streaming");

    try {
      const res = await fetch("/api/analyse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: repoUrl }),
      });
      if (!res.ok || !res.body) {
        const detail = await res.text().catch(() => "");
        throw new Error(detail || `Request failed (${res.status})`);
      }
      await readStream(res.body);
    } catch (err) {
      showError(err.message || String(err));
    }
  }

  // ---- SSE stream reader --------------------------------------------------
  async function readStream(body) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Split on the SSE event separator. sse-starlette uses "\r\n\r\n" by
      // default; we also accept "\n\n". Splitting the whole buffer (rather
      // than normalizing per-chunk) keeps separators that straddle a chunk
      // boundary intact.
      const frames = buffer.split(/\r\n\r\n|\n\n/);
      buffer = frames.pop(); // keep incomplete trailing frame

      for (const frame of frames) {
        // An SSE event may carry multiple "data:" lines — concatenate them.
        const data = frame
          .split(/\r?\n/)
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).replace(/^ /, ""))
          .join("\n")
          .trim();
        if (!data) continue;
        try {
          handleEvent(JSON.parse(data));
        } catch {
          /* ignore malformed / non-JSON frame (e.g. ping comments) */
        }
      }
    }
  }

  function handleEvent(evt) {
    const { type, content } = evt;
    switch (type) {
      case "job":
        jobId.current = content;
        break;
      case "stage":
        appendStage(content);
        break;
      case "message":
        appendText(content);
        break;
      case "questions":
        beginQA(content);
        break;
      case "complete":
        finishRun(content);
        break;
      case "error":
        showError(content);
        break;
      default:
        break;
    }
  }

  function appendStage(text) {
    const id = currentAssistant.current;
    if (id == null) return;
    updateMessage(id, (m) => ({ stages: [...m.stages, text] }));
  }

  function appendText(text) {
    const id = currentAssistant.current;
    if (id == null) return;
    updateMessage(id, (m) => ({
      text: m.text ? `${m.text}\n${text}` : text,
    }));
  }

  // ---- interactive Q&A ----------------------------------------------------
  function beginQA(qs) {
    // Finalize the pre-question bubble.
    if (currentAssistant.current != null) {
      updateMessage(currentAssistant.current, () => ({ streaming: false }));
    }
    questions.current = qs || [];
    qaIndex.current = 0;
    setPhase("qa");
    askNextQuestion();
  }

  function askNextQuestion() {
    const i = qaIndex.current;
    const qs = questions.current;
    if (i >= qs.length) return;
    addMessage({
      role: "assistant",
      kind: "question",
      text: qs[i],
      qLabel: `Question ${i + 1} of ${qs.length}`,
    });
  }

  async function submitAnswer(answer) {
    addMessage({ role: "user", text: answer });
    try {
      const res = await fetch("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId.current, answer }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Failed to send answer");

      qaIndex.current += 1;
      if ((data.remaining ?? 0) > 0 && qaIndex.current < questions.current.length) {
        askNextQuestion();
      } else {
        // All answered — resume streaming into a fresh assistant bubble.
        currentAssistant.current = addMessage({
          role: "assistant",
          streaming: true,
        });
        setPhase("streaming");
      }
    } catch (err) {
      showError(err.message || String(err));
    }
  }

  // ---- terminal states ----------------------------------------------------
  function finishRun(note) {
    const id = currentAssistant.current;
    if (id != null) {
      updateMessage(id, (m) => ({
        streaming: false,
        showDownload: true,
        // The accumulated report text is markdown — render it as such.
        isReport: !!m.text,
        text: m.text ? m.text : note || "Analysis complete.",
      }));
    }
    setPhase("idle");
  }

  function showError(text) {
    const id = currentAssistant.current;
    if (id != null) {
      updateMessage(id, () => ({ kind: "error", streaming: false, text }));
    } else {
      addMessage({ role: "assistant", kind: "error", text });
    }
    setPhase("idle");
  }

  async function downloadReport() {
    try {
      const res = await fetch("/api/report");
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "Report not available");
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "deployguard-report.md";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      showError(err.message || String(err));
    }
  }

  const placeholder =
    phase === "qa"
      ? "Type your answer and press Enter..."
      : "Paste a GitHub repo URL (https://github.com/...)";

  return (
    <div className="app">
      <header className="header">
        <span className="logo">◈ DeployGuard</span>
        <span className="tagline">Pre-deployment reliability analyser</span>
      </header>

      <main className="chat" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty">
            <p>Submit a public GitHub repository URL to begin a reliability analysis.</p>
            <p className="empty-sub">
              DeployGuard parses the code, runs an SRE rules engine, reasons with an
              LLM, simulates load, and produces a downloadable risk report.
            </p>
          </div>
        )}

        {messages.map((m) => (
          <Bubble key={m.id} m={m} onDownload={downloadReport} />
        ))}

        {busy && <TypingIndicator />}
      </main>

      <form className="composer" onSubmit={handleSubmit}>
        <input
          className="composer-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={placeholder}
          disabled={busy}
          autoFocus
        />
        <button className="composer-send" type="submit" disabled={busy || !input.trim()}>
          {phase === "qa" ? "Answer" : "Analyse"}
        </button>
      </form>
    </div>
  );
}

function Bubble({ m, onDownload }) {
  const isUser = m.role === "user";
  const cls = [
    "bubble",
    isUser ? "bubble-user" : "bubble-assistant",
    m.kind === "error" ? "bubble-error" : "",
    m.kind === "question" ? "bubble-question" : "",
    m.isReport ? "bubble-report" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={`row ${isUser ? "row-user" : "row-assistant"}`}>
      <div className={cls}>
        {m.qLabel && <div className="q-label">{m.qLabel}</div>}

        {m.stages?.map((s, i) => (
          <div className="stage" key={i}>
            <span className="stage-spin">⟳</span> {s}
          </div>
        ))}

        {m.text &&
          (m.isReport ? (
            <div className="markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown>
            </div>
          ) : (
            <div className="bubble-text">{m.text}</div>
          ))}

        {m.showDownload && (
          <button className="download-btn" onClick={onDownload}>
            ⭳ Download Report (.md)
          </button>
        )}
      </div>
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="row row-assistant">
      <div className="bubble bubble-assistant typing">
        <span className="dot" />
        <span className="dot" />
        <span className="dot" />
      </div>
    </div>
  );
}
