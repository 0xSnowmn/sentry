import {useState} from 'react';
import {createBrowserRouter, RouterProvider} from 'react-router-dom';
import {wrapCreateBrowserRouter} from '@sentry/react';
import {ReactQueryDevtools} from '@tanstack/react-query-devtools';

import DemoHeader from 'sentry/components/demo/demoHeader';
import {OnboardingContextProvider} from 'sentry/components/onboarding/onboardingContext';
import {ThemeAndStyleProvider} from 'sentry/components/themeAndStyleProvider';
import {routes} from 'sentry/routes';
import {DANGEROUS_SET_REACT_ROUTER_6_HISTORY} from 'sentry/utils/browserHistory';
import {
  DEFAULT_QUERY_CLIENT_CONFIG,
  QueryClient,
  QueryClientProvider,
} from 'sentry/utils/queryClient';

import {buildReactRouter6Routes} from './utils/reactRouter6Compat/router';

const queryClient = new QueryClient(DEFAULT_QUERY_CLIENT_CONFIG);

function buildRouter() {
  const sentryCreateBrowserRouter = wrapCreateBrowserRouter(createBrowserRouter);
  const router = sentryCreateBrowserRouter(buildReactRouter6Routes(routes()));
  DANGEROUS_SET_REACT_ROUTER_6_HISTORY(router);

  return router;
}

function Main() {
  const [router] = useState(buildRouter);

  return (
    <ThemeAndStyleProvider>
      <QueryClientProvider client={queryClient}>
        <OnboardingContextProvider>
          <DemoHeader />
          <RouterProvider router={router} />
        </OnboardingContextProvider>
        <ReactQueryDevtools initialIsOpen={false} buttonPosition="bottom-left" />
      </QueryClientProvider>
    </ThemeAndStyleProvider>
  );
}

export default Main;
