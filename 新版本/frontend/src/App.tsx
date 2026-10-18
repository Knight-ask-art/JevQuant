import { Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { AgentDesk } from "./pages/AgentDesk";
import { AuditSettings } from "./pages/AuditSettings";
import { Backtests } from "./pages/Backtests";
import { Cockpit } from "./pages/Cockpit";
import { JevSignals } from "./pages/JevSignals";
import { ModelsCosts } from "./pages/ModelsCosts";
import { OrdersPositions } from "./pages/OrdersPositions";
import { Overview } from "./pages/Overview";
import { RiskModes } from "./pages/RiskModes";
import { StrategyDesk } from "./pages/StrategyDesk";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Cockpit />} />
        <Route path="agent" element={<AgentDesk />} />
        <Route path="overview" element={<Overview />} />
        <Route path="strategy" element={<StrategyDesk />} />
        <Route path="signals" element={<JevSignals />} />
        <Route path="orders" element={<OrdersPositions />} />
        <Route path="backtests" element={<Backtests />} />
        <Route path="models" element={<ModelsCosts />} />
        <Route path="risk" element={<RiskModes />} />
        <Route path="audit" element={<AuditSettings />} />
      </Route>
    </Routes>
  );
}
