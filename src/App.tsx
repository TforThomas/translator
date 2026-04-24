import { BrowserRouter, Routes, Route } from "react-router-dom";
import LingoShell from "./components/LingoShell";
import Home from "./pages/Home";
import TaskDetail from "./pages/TaskDetail";
import TermsManagement from "./pages/TermsManagement";
import Settings from "./pages/Settings";

export default function App() {
	return (
		<BrowserRouter>
			<LingoShell>
				<Routes>
					<Route path="/" element={<Home />} />
					<Route path="/task/:id" element={<TaskDetail />} />
					<Route path="/terms" element={<TermsManagement />} />
					<Route path="/settings" element={<Settings />} />
				</Routes>
			</LingoShell>
		</BrowserRouter>
	);
}