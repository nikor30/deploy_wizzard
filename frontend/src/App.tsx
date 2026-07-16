import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import Placeholder from './components/Placeholder'
import SettingsCredentials from './pages/SettingsCredentials'
import SettingsMapping from './pages/SettingsMapping'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Placeholder title="Onboarding Wizard" phase="P3" />} />
        <Route path="settings/credentials" element={<SettingsCredentials />} />
        <Route path="settings/mapping" element={<SettingsMapping />} />
        <Route path="settings/dayn" element={<Placeholder title="Day-N Variables" phase="P5" />} />
        <Route path="stats" element={<Placeholder title="Statistics" phase="P6" />} />
        <Route path="logs" element={<Placeholder title="Logs" phase="P6" />} />
      </Route>
    </Routes>
  )
}
