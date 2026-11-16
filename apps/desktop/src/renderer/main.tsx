import { createRoot } from 'react-dom/client'

import App from './App'
import { Pet } from './Pet'
import './styles.css'

const container = document.getElementById('root')
if (!container) throw new Error('#root missing from index.html')

const surface = new URLSearchParams(window.location.search).get('surface')

// No StrictMode: it double-invokes effects, which would create and tear down
// a second WebGL context on every mount.
createRoot(container).render(surface === 'pet' ? <Pet /> : <App />)
