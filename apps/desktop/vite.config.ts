import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '../..')

export default defineConfig({
  root: path.join(here, 'src/renderer'),
  base: './',
  plugins: [react()],
  resolve: {
    alias: {
      '@packs': path.join(repoRoot, 'packs'),
      '@': path.join(here, 'src/renderer'),
    },
  },
  server: {
    port: 5273,
    strictPort: true,
    fs: { allow: [repoRoot] },
  },
  build: {
    outDir: path.join(here, 'dist'),
    emptyOutDir: true,
    target: 'esnext',
    sourcemap: true,
  },
})
