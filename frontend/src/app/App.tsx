import { Navigate, Route, Routes } from "react-router-dom";
import TodayPage from "../features/today/TodayPage";

export default function App() {
  return (
    <Routes>
      <Route path="/today" element={<TodayPage />} />
      <Route path="/" element={<Navigate to="/today" replace />} />
      <Route path="*" element={<Navigate to="/today" replace />} />
    </Routes>
  );
}
