import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import skipFormatting from '@vue/eslint-config-prettier/skip-formatting'

export default defineConfigWithVueTs(
  { ignores: ['dist/**', 'node_modules/**', '**/*.d.ts'] },
  js.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  // v14+ 导出的是单个 config 对象，不是数组，不能展开。
  vueTsConfigs.recommended,
  skipFormatting,
  {
    // public/ 下是不进构建图的独立页面脚本（Tauri 启动失败页），只有浏览器全局。
    files: ['public/**/*.js'],
    languageOptions: {
      globals: { window: 'readonly', document: 'readonly' },
    },
  },
  {
    rules: {
      'vue/multi-word-component-names': 'off',
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },
)
