import { DashboardGrid } from "@/components/DashboardGrid";

export default function Page() {
  return (
    <main className="app">
      <header className="app-header">
        <h1>Market Terminal</h1>
        <span className="tag">local · private · free data · Phase 0 skeleton</span>
      </header>
      <DashboardGrid />
    </main>
  );
}
