"""Authentication (Phase 7A, D030): OIDC sign-in, server-side sessions, CSRF.

Provider-specific code lives only in oidc.py. Everything else works with the resulting
User row and session, so organization authorization never depends on the provider.
"""
