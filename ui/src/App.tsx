import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import ConvergeDetail from "./pages/ConvergeDetail";
import Triage from "./pages/Triage";

const styles = {
  nav: {
    background: "#161b22",
    padding: "12px 24px",
    borderBottom: "1px solid #30363d",
    display: "flex",
    gap: "24px",
    alignItems: "center",
  } as React.CSSProperties,
  brand: {
    fontWeight: 700,
    fontSize: "18px",
    color: "#58a6ff",
    textDecoration: "none",
  } as React.CSSProperties,
  link: {
    color: "#8b949e",
    textDecoration: "none",
    fontSize: "14px",
  } as React.CSSProperties,
  main: {
    padding: "24px",
    maxWidth: "1200px",
    margin: "0 auto",
  } as React.CSSProperties,
};

export default function App() {
  return (
    <BrowserRouter>
      <nav style={styles.nav}>
        <Link to="/" style={styles.brand}>
          🤖 Orchestrator
        </Link>
        <Link to="/" style={styles.link}>
          Dashboard
        </Link>
        <Link to="/runs" style={styles.link}>
          Runs
        </Link>
        <Link to="/triage" style={styles.link}>
          Triage
        </Link>
      </nav>
      <main style={styles.main}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:run_id" element={<RunDetail />} />
          <Route path="/prs/:owner/:repo/:number/converge" element={<ConvergeDetail />} />
          <Route path="/triage" element={<Triage />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}
