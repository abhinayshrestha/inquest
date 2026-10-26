import { lazy, Suspense, type ComponentType, type LazyExoticComponent } from 'react'
import type { RouteObject } from 'react-router'

import { PageSkeleton, type PageSkeletonVariant } from '@/components/skeletons'

import { AppShell } from './layout/app-shell'
import { NotFoundPage } from './not-found-page'
import { RouteErrorPage } from './route-error-page'

const OverviewPage = lazy(() => import('@/features/overview/overview-page'))
const InvestigationsPage = lazy(() => import('@/features/investigations/investigations-page'))
const InvestigationDetailPage = lazy(
  () => import('@/features/investigations/investigation-detail-page'),
)
const NewInvestigationPage = lazy(() => import('@/features/investigations/new-investigation-page'))
const ReportPage = lazy(() => import('@/features/investigations/report-page'))
const EnvironmentsPage = lazy(() => import('@/features/environments/environments-page'))
const EnvironmentDetailPage = lazy(() => import('@/features/environments/environment-detail-page'))
const ReportsPage = lazy(() => import('@/features/reports/reports-page'))
const OrganizationsPage = lazy(() => import('@/features/organizations/organizations-page'))
const SettingsPage = lazy(() => import('@/features/settings/settings-page'))

// Each page gets a layout-shaped Suspense fallback and its own errorElement, so a failing
// page (or chunk) renders inside the shell and navigation keeps working.
function page(
  path: string | undefined,
  Page: LazyExoticComponent<ComponentType>,
  skeleton: PageSkeletonVariant,
): RouteObject {
  const element = (
    <Suspense fallback={<PageSkeleton variant={skeleton} />}>
      <Page />
    </Suspense>
  )
  return path === undefined
    ? { index: true, element, errorElement: <RouteErrorPage /> }
    : { path, element, errorElement: <RouteErrorPage /> }
}

export const routes: RouteObject[] = [
  {
    element: <AppShell />,
    errorElement: <RouteErrorPage />,
    children: [
      page(undefined, OverviewPage, 'cards'),
      page('investigations', InvestigationsPage, 'list'),
      page('investigations/new', NewInvestigationPage, 'detail'),
      page('investigations/:id', InvestigationDetailPage, 'detail'),
      page('investigations/:id/reports/:version', ReportPage, 'detail'),
      page('environments', EnvironmentsPage, 'list'),
      page('environments/:id', EnvironmentDetailPage, 'detail'),
      page('reports', ReportsPage, 'list'),
      page('organizations', OrganizationsPage, 'cards'),
      page('settings', SettingsPage, 'cards'),
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
