import js from '@eslint/js'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default defineConfig([
  globalIgnores(['dist', 'coverage']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat['recommended-latest'],
      reactRefresh.configs.vite,
      jsxA11y.flatConfigs.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
    },
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      'no-restricted-syntax': [
        'error',
        {
          // Model output must never become markup (D023).
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: 'Render content through validated components, never raw HTML.',
        },
      ],
    },
  },
  {
    // shadcn/ui primitives export variants alongside components; contexts export their
    // hook next to the provider; routes.tsx is route configuration. Fast refresh falls back to a full reload there.
    files: ['src/components/ui/**', 'src/app/contexts/**', 'src/app/routes.tsx'],
    rules: { 'react-refresh/only-export-components': 'off' },
  },
])
