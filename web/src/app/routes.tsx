import { lazy, Suspense, type ComponentType, type LazyExoticComponent } from 'react'
import type { RouteObject } from 'react-router'

import { PageSkeleton, type PageSkeletonVariant } from '@/components/skeletons'
import { LOGIN_PATH } from '@/features/auth/login-path'
import { FullPageLoader, RequireAuth } from '@/features/auth/require-auth'
import { RequireOrganization } from '@/features/organizations/organization-gate'

import { AppShell } from './layout/app-shell'
import { NotFoundPage } from './not-found-page'
import { RouteErrorPage } from './route-error-page'

const LoginPage = lazy(() => import('@/features/auth/login-page'))
const OverviewPage = lazy(() => import('@/features/overview/overview-page'))
const InvestigationsPage = lazy(() => import('@/features/investigations/investigations-page'))
const InvestigationDetailPage = lazy(
  () => import('@/features/investigations/investigation-detail-page'),
)
const NewInvestigationPage = lazy(() => import('@/features/investigations/new-investigation-page'))
const ReportPage = lazy(() => import('@/features/reports/report-view/report-page'))
const EnvironmentsPage = lazy(() => import('@/features/environments/environments-page'))
const EnvironmentDetailPage = lazy(() => import('@/features/environments/environment-detail-page'))
const ReportsPage = lazy(() => import('@/features/reports/reports-page'))
const OrganizationsPage = lazy(() => import('@/features/organizations/organizations-page'))
const OrganizationDetailPage = lazy(
  () => import('@/features/organizations/organization-detail-page'),
)
const AcceptInvitationPage = lazy(() => import('@/features/organizations/accept-invitation-page'))
const SettingsPage = lazy(() => import('@/features/settings/settings-page'))

// Pages of one organization's data: signed-in members with an organization selected.
const TENANT = { tenant: true }

// Each page gets a layout-shaped Suspense fallback and its own errorElement, so a failing
// page (or chunk) renders inside the shell and navigation keeps working.
function page(
  path: string | undefined,
  Page: LazyExoticComponent<ComponentType>,
  skeleton: PageSkeletonVariant,
  { tenant = false } = {},
): RouteObject {
  const content = <Page />
  const element = (
    <Suspense fallback={<PageSkeleton variant={skeleton} />}>
      {tenant ? <RequireOrganization>{content}</RequireOrganization> : content}
    </Suspense>
  )
  return path === undefined
    ? { index: true, element, errorElement: <RouteErrorPage /> }
    : { path, element, errorElement: <RouteErrorPage /> }
}

export const routes: RouteObject[] = [
  {
    path: LOGIN_PATH,
    element: (
      <Suspense fallback={<FullPageLoader label="Loading" />}>
        <LoginPage />
      </Suspense>
    ),
    errorElement: <RouteErrorPage />,
  },
  // Invitation links must open signed out: the page starts its own invitation sign-in.
  {
    element: <AppShell />,
    errorElement: <RouteErrorPage />,
    children: [page('invitations/accept', AcceptInvitationPage, 'detail')],
  },
  {
    element: (
      <RequireAuth>
        <AppShell />
      </RequireAuth>
    ),
    errorElement: <RouteErrorPage />,
    children: [
      page(undefined, OverviewPage, 'cards', TENANT),
      page('investigations', InvestigationsPage, 'list', TENANT),
      page('investigations/new', NewInvestigationPage, 'detail', TENANT),
      page('investigations/:id', InvestigationDetailPage, 'detail', TENANT),
      page('investigations/:id/reports/:version', ReportPage, 'detail', TENANT),
      page('environments', EnvironmentsPage, 'list', TENANT),
      page('environments/:id', EnvironmentDetailPage, 'detail', TENANT),
      page('reports', ReportsPage, 'list', TENANT),
      page('organizations', OrganizationsPage, 'cards'),
      page('organizations/:orgId', OrganizationDetailPage, 'detail'),
      page('settings', SettingsPage, 'cards'),
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
