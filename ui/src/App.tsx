/**
 * App root — router + navigation (WEBUI.md §4).
 *
 * Navigation:
 * - Mobile (≤640px): bottom navigation bar, 5 items
 * - Desktop (≥1025px): persistent sidebar
 * - Tablet: bottom bar (compact)
 *
 * Auth guard: /login is public; all other routes require a valid JWT.
 * 401 responses in api.ts redirect to /login automatically.
 */
import { useEffect, useState } from "react";
import { BrowserRouter, Link, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { getToken, startProactiveRefresh } from "./auth";
import ConvergeDetail from "./pages/ConvergeDetail";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import Repos from "./pages/Repos";
import RunDetail from "./pages/RunDetail";
import Runs from "./pages/Runs";
import Settings from "./pages/Settings";
import Triage from "./pages/Triage";

// Navigation items (WEBUI.md §4)
const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: "⬛", exact: true },
  { to: "/triage", label: "Triage", icon: "📋", exact: false },
  { to: "/repos", label: "Repos", icon: "🗂", exact: false },
  { to: "/runs", label: "Runs", icon: "▶", exact: false },
  { to: "/settings", label: "Settings", icon: "⚙", exact: false },
] as const;

const navStyle: React.CSSProperties = {
  background: "#161b22",
  borderTop: "1px solid #30363d",
  display: "flex",
  position: "fixed",
  bottom: 0,
  left: 0,
  right: 0,
  zIndex: 100,
  padding: "0",
};

function navItemStyle(active: boolean): React.CSSProperties {
  return {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    padding: "8px 4px",
    textDecoration: "none",
    color: active ? "#58a6ff" : "#8b949e",
    fontSize: "10px",
    minHeight: "56px",
    gap: "4px",
    borderTop: active ? "2px solid #58a6ff" : "2px solid transparent",
    transition: "color 0.15s",
  };
}

const sidebarStyle: React.CSSProperties = {
  width: "220px",
  background: "#161b22",
  borderRight: "1px solid #30363d",
  display: "flex",
  flexDirection: "column",
  padding: "0",
  position: "fixed",
  top: 0,
  left: 0,
  bottom: 0,
  zIndex: 100,
};

function sidebarItemStyle(active: boolean): React.CSSProperties {
  return {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    padding: "12px 20px",
    textDecoration: "none",
    color: active ? "#e6edf3" : "#8b949e",
    background: active ? "rgba(88,166,255,0.1)" : "transparent",
    borderLeft: active ? "3px solid #58a6ff" : "3px solid transparent",
    fontSize: "14px",
    fontWeight: active ? 600 : 400,
    transition: "background 0.15s, color 0.15s",
  };
}

function useIsDesktop() {
  const [desktop, setDesktop] = useState(window.innerWidth >= 1025);
  useEffect(() => {
    const fn = () => setDesktop(window.innerWidth >= 1025);
    window.addEventListener("resize", fn);
    return () => window.removeEventListener("resize", fn);
  }, []);
  return desktop;
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (!getToken()) {
      const next = encodeURIComponent(location.pathname + location.search);
      void navigate(`/login?next=${next}`, { replace: true });
    }
  });

  // Proactive token refresh: start the background timer while the user is
  // authenticated. The cleanup function stops the interval on logout (which
  // unmounts RequireAuth when the router transitions to /login) so no
  // refresh attempts happen after the token has been cleared.
  useEffect(() => {
    if (!getToken()) return;
    return startProactiveRefresh();
  }, []); // mount-once; startProactiveRefresh is stable (no closure over props)

  if (!getToken()) return null;
  return <>{children}</>;
}

function BottomNav() {
  const location = useLocation();
  return (
    <nav style={navStyle} aria-label="Main navigation">
      {NAV_ITEMS.map((item) => {
        const active = item.exact
          ? location.pathname === item.to
          : location.pathname.startsWith(item.to);
        return (
          <NavLink
            key={item.to}
            to={item.to}
            style={navItemStyle(active)}
            aria-current={active ? "page" : undefined}
            aria-label={item.label}
          >
            <span aria-hidden="true" style={{ fontSize: "18px" }}>
              {item.icon}
            </span>
            <span>{item.label}</span>
          </NavLink>
        );
      })}
    </nav>
  );
}

function Sidebar() {
  const location = useLocation();
  return (
    <nav style={sidebarStyle} aria-label="Main navigation">
      <div
        style={{
          padding: "20px 20px 16px",
          borderBottom: "1px solid #30363d",
          fontWeight: 700,
          fontSize: "16px",
          color: "#58a6ff",
        }}
      >
        <Link
          to="/"
          style={{ textDecoration: "none", color: "inherit" }}
          aria-label="Orchestrator home"
        >
          Orchestrator
        </Link>
      </div>
      <ul style={{ listStyle: "none", flex: 1, padding: "8px 0" }}>
        {NAV_ITEMS.map((item) => {
          const active = item.exact
            ? location.pathname === item.to
            : location.pathname.startsWith(item.to);
          return (
            <li key={item.to}>
              <NavLink
                to={item.to}
                style={sidebarItemStyle(active)}
                aria-current={active ? "page" : undefined}
              >
                <span aria-hidden="true">{item.icon}</span>
                <span>{item.label}</span>
              </NavLink>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function AppShell({ children }: { children: React.ReactNode }) {
  const isDesktop = useIsDesktop();
  const contentStyle: React.CSSProperties = {
    flex: 1,
    marginLeft: isDesktop ? "220px" : "0",
    paddingBottom: !isDesktop ? "72px" : "0",
    minHeight: "100vh",
  };

  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      {isDesktop ? <Sidebar /> : null}
      <div style={contentStyle}>{children}</div>
      {!isDesktop ? <BottomNav /> : null}
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Public route — no auth guard */}
        <Route path="/login" element={<Login />} />

        {/* Protected routes */}
        <Route
          path="/*"
          element={
            <RequireAuth>
              <AppShell>
                <Routes>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/runs" element={<Runs />} />
                  <Route path="/runs/:run_id" element={<RunDetail />} />
                  <Route
                    path="/prs/:owner/:repo/:number/converge"
                    element={<ConvergeDetail />}
                  />
                  <Route path="/triage" element={<Triage />} />
                  <Route path="/repos" element={<Repos />} />
                  <Route path="/settings" element={<Settings />} />
                </Routes>
              </AppShell>
            </RequireAuth>
          }
        />
      </Routes>
    </BrowserRouter>
  );
}
