import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { UIModeProvider } from './hooks/useUiMode'
import './index.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <UIModeProvider>
      <App />
    </UIModeProvider>
  </StrictMode>,
)
