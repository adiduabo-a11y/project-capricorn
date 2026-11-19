import { Component, type ErrorInfo, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import { Pet } from './Pet'
import './styles.css'

interface BoundaryState {
  error: Error | null
}

class ErrorBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[capri] render error', error, info.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div
        style={{
          padding: 16,
          fontFamily: 'system-ui, sans-serif',
          color: '#f5eef2',
          background: 'rgba(24, 18, 28, 0.94)',
          border: '1px solid rgba(255, 110, 140, 0.45)',
          borderRadius: 10,
          margin: 8,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 6 }}>Surface crashed</div>
        <div style={{ fontSize: 12, opacity: 0.85, marginBottom: 10 }}>
          {this.state.error.message}
        </div>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{ padding: '4px 10px', cursor: 'pointer' }}
        >
          Reload
        </button>
      </div>
    )
  }
}

const container = document.getElementById('root')
if (!container) throw new Error('#root missing from index.html')

const surface = new URLSearchParams(window.location.search).get('surface')

// No StrictMode: it double-invokes effects, which would create and tear down
// a second WebGL context on every mount.
createRoot(container).render(
  <ErrorBoundary>{surface === 'pet' ? <Pet /> : <App />}</ErrorBoundary>,
)
