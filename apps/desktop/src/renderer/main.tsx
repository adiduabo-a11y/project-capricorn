import { createRoot } from 'react-dom/client'

import App from './App'
import './styles.css'

const container = document.getElementById('root')
if (!container) throw new Error('#root missing from index.html')

// No StrictMode: it double-invokes effects, which would create and tear down
// a second WebGL context on every mount.
createRoot(container).render(<App />)
