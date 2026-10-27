import {defineConfig} from 'vite';
import motionCanvasModule from '@motion-canvas/vite-plugin';

// The plugin is CommonJS; depending on the loader the factory sits on `.default`.
const motionCanvas = (motionCanvasModule as any).default ?? motionCanvasModule;

export default defineConfig({
  plugins: [motionCanvas()],
  server: {port: 9000},
});
