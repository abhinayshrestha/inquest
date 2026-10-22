import { Component, type ErrorInfo, type ReactNode } from 'react'

import { ErrorState } from './error-state'

type Props = {
  children: ReactNode
  fallback?: (error: unknown, reset: () => void) => ReactNode
  // Changing any key clears the error, e.g. after navigation.
  resetKeys?: readonly unknown[]
}

type State = { error: unknown; hasError: boolean }

// A class because React only supports error boundaries as class components.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, hasError: false }

  static getDerivedStateFromError(error: unknown): State {
    return { error, hasError: true }
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    if (import.meta.env.DEV) console.error('ErrorBoundary caught', error, info.componentStack)
  }

  componentDidUpdate(previous: Props) {
    const keys = this.props.resetKeys ?? []
    const before = previous.resetKeys ?? []
    if (
      this.state.hasError &&
      (keys.length !== before.length || keys.some((key, i) => !Object.is(key, before[i])))
    ) {
      this.reset()
    }
  }

  reset = () => this.setState({ error: null, hasError: false })

  render() {
    if (!this.state.hasError) return this.props.children
    if (this.props.fallback) return this.props.fallback(this.state.error, this.reset)
    return <ErrorState error={this.state.error} onRetry={this.reset} />
  }
}
