import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import SettingsCredentials from './pages/SettingsCredentials'
import Logs from './pages/Logs'
import SettingsDayN from './pages/SettingsDayN'
import Stats from './pages/Stats'
import SettingsMapping from './pages/SettingsMapping'
import Wizard from './pages/Wizard'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Wizard />} />
        <Route path="settings/credentials" element={<SettingsCredentials />} />
        <Route path="settings/mapping" element={<SettingsMapping />} />
        <Route path="settings/dayn" element={<SettingsDayN />} />
        <Route path="stats" element={<Stats />} />
        <Route path="logs" element={<Logs />} />
      </Route>
    </Routes>
  )
}
